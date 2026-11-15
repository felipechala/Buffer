"""deepagents orchestrator for the stress search.

The LLM may only *choose* which deterministic searches to run (and on which shock family). It never
produces numbers: every elite, shortfall, plausibility and fixability verdict comes from the engine
(`agent.search`, `engine.simulator`, `engine.optimizer`). After the agent finishes (or fails), the
authoritative StressRun is always produced by the deterministic `runs.execute_run`, so a deepagent run
has the same elites as a deterministic run with the same seed/budget plus a `narrative` trace of what
the agent explored. Any failure -> `agent="fallback"` with a reason, and the deterministic run proceeds.

`deepagents` / `langchain-anthropic` are imported lazily; the app and tests work without them.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable

from ..engine.models import TwinState
from ..engine.optimizer import optimize_plan
from ..engine.simulator import simulate
from ..workflows.forecast import build_twin_from_store
from ..workflows.store import Store
from .runs import StressRun, build_probes, execute_run, _save_run
from .search import Evaluator, assess_fixable, search
from .shocks import ShockBounds, ShockSpace, ShockVector, describe, shock_to_scenario

log = logging.getLogger("cashguard.deep")

DEFAULT_AGENT_MODEL = "claude-sonnet-5-5"
MAX_TOOL_CALLS = 12
MAX_SECONDS = 60.0
MAX_SEARCH_BUDGET = 60
FOCI = ("all", "receivables", "routes", "outflows")


class AgentUnavailable(RuntimeError):
    pass


def _usd(cents: int) -> float:
    return round(cents / 100, 2)


def _focus_bounds(base: ShockBounds, focus: str) -> ShockBounds:
    """Restrict the shock family. Disabled families collapse to 'no shock' of that kind."""
    if focus == "receivables":
        return base.model_copy(update={"surprise_outflow_max_cents": 0, "route_outage": False, "credit_outage": False})
    if focus == "routes":
        return base.model_copy(update={"receipt_delay_max_days": 0, "collection_fraction_min": 1.0,
                                       "surprise_outflow_max_cents": 0, "credit_outage": True, "route_outage": True})
    if focus == "outflows":
        return base.model_copy(update={"receipt_delay_max_days": 0, "collection_fraction_min": 1.0,
                                       "route_outage": False, "credit_outage": False})
    return base


class ToolContext:
    """Deterministic tools the agent can call. Enforces the call and time caps and records the trace."""

    def __init__(self, store: Store, *, seed: int, max_calls: int = MAX_TOOL_CALLS, max_seconds: float = MAX_SECONDS):
        self.store = store
        self.seed = seed
        self.base: TwinState = build_twin_from_store(store)
        self.bounds = ShockBounds.from_config(store.get_config().shock_bounds)
        self.probes = build_probes(store, self.base)
        self.max_calls, self.deadline = max_calls, time.monotonic() + max_seconds
        self.calls = 0
        self.steps: list[dict[str, Any]] = []
        self.found: dict[str, Any] = {}           # elite scenario id -> Scenario
        self._lock = threading.Lock()
        self.cancelled = False

    # ---- bookkeeping
    def _guard(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.cancelled or time.monotonic() > self.deadline:
                raise AgentUnavailable("agent time budget exhausted")
            if self.calls >= self.max_calls:
                return {"error": f"tool-call budget of {self.max_calls} exhausted; write your final summary now"}
            self.calls += 1
            step = {"step": len(self.steps) + 1, "tool": tool, "args": args}
            self.steps.append(step)
        return step

    def _done(self, step: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        step["result"] = result
        return result

    # ---- tools
    def list_shocks(self) -> dict[str, Any]:
        """Describe what a shock may touch in this company's twin and the plausible bounds."""
        step = self._guard("list_shocks", {})
        if "error" in step:
            return step
        space = ShockSpace.from_twin(self.base, self.bounds)
        return self._done(step, {
            "receivable_keys": list(space.keys), "routes": list(space.routes), "credit_lines": list(space.credit_lines),
            "accounts": list(space.accounts), "horizon_days": space.horizon, "foci": list(FOCI),
            "bounds": self.bounds.model_dump(), "payment_postures": [p.id for p in self.probes]})

    def get_history(self, customer: str) -> dict[str, Any]:
        """Payment-delay history (delay distribution) for receivables whose counterparty matches `customer`."""
        step = self._guard("get_history", {"customer": customer})
        if "error" in step:
            return step
        rows = [{"receipt": r.id, "counterparty": r.counterparty, "amount_usd": _usd(r.amount_cents),
                 "expected_day": r.expected_day, "delay_distribution": {str(k): v for k, v in r.delay_distribution.items()}}
                for r in self.base.receipts if customer.lower() in (r.counterparty or r.id).lower()]
        return self._done(step, {"customer": customer, "receipts": rows[:8]})

    def search_shocks(self, budget: int = 30, focus: str = "all") -> dict[str, Any]:
        """Run the deterministic adaptive risk search restricted to one shock family; returns the top findings."""
        budget = max(5, min(int(budget), MAX_SEARCH_BUDGET))
        focus = focus if focus in FOCI else "all"
        step = self._guard("search_shocks", {"budget": budget, "focus": focus})
        if "error" in step:
            return step
        bounds = _focus_bounds(self.bounds, focus)
        res = search(self.probes, bounds, budget=budget, seed=self.seed)
        ev = Evaluator(self.probes, ShockSpace.from_twin(self.base, bounds))
        top = []
        probes = {p.id: p.twin for p in self.probes}
        for e in [x for x in res.elites if x.breach and x.plausibility > 0][:3]:
            sid = f"{focus}-{e.k}"
            scen = ev.scenario(e.shock, e.k).model_copy(update={"id": sid})
            self.found[sid] = scen
            fx = assess_fixable(probes[e.probe], scen)
            top.append({"stress_id": sid, "label": scen.description, "probe": e.probe,
                        "worst_shortfall_usd": _usd(e.worst_shortfall_cents), "plausibility": round(e.plausibility, 6),
                        "fixable": fx.fixable})
        return self._done(step, {"focus": focus, "evaluations": res.evaluations,
                                 "first_breach_found_at": res.first_breach_found_at, "findings": top})

    def simulate(self, scenario: dict[str, Any]) -> dict[str, Any]:
        """Replay a shock (delays / haircuts / outflow / disabled_route / disabled_credit) on every posture."""
        step = self._guard("simulate", {"scenario": scenario})
        if "error" in step:
            return step
        try:
            shock = ShockVector.model_validate(scenario)
        except Exception as exc:  # noqa: BLE001
            return self._done(step, {"error": f"invalid scenario: {exc}"})
        scen = shock_to_scenario(self.base, shock, "agent-sim")
        out = []
        for p in self.probes:
            m = simulate(p.twin, scen).metrics
            out.append({"probe": p.id, "breach": m.breach, "worst_shortfall_usd": _usd(m.worst_shortfall_cents)})
        return self._done(step, {"description": describe(self.base, shock), "postures": out})

    def optimize(self, stress_ids: list[str]) -> dict[str, Any]:
        """Robust transfer/credit plan against previously found stress ids (from search_shocks)."""
        step = self._guard("optimize", {"stress_ids": stress_ids})
        if "error" in step:
            return step
        scens = [self.found[i] for i in stress_ids if i in self.found]
        if not scens:
            return self._done(step, {"error": "no known stress_ids; call search_shocks first"})
        plan = optimize_plan(self.base, stress=scens)
        return self._done(step, {"status": plan.status, "actions": len(plan.actions),
                                 "modeled_cost_usd": _usd(plan.modeled_cost_cents), "stress_ids": stress_ids})


# ------------------------------------------------------------------ agent
SYSTEM_PROMPT = (
    "You are the risk-search orchestrator for Buffer. Your job is to decide WHICH deterministic stress searches "
    "to run, not to compute anything. Call list_shocks first, then delegate to the subagents (receivables-analyst, "
    "routes-analyst, outflows-analyst) or call search_shocks yourself with a focus in "
    "{all, receivables, routes, outflows} and a small budget (<=40). You may call get_history, simulate and optimize "
    "to follow up. You have at most 12 tool calls. Every number in your summary must be quoted from tool results; "
    "never invent or compute figures. Finish with 3-4 plain sentences naming the most dangerous finding "
    "(dollar shortfall and label from the tools) and whether a plan can fix it."
)

SUBAGENTS = [
    ("receivables-analyst", "Probes late/short customer payments (receivable delays and haircuts).", "receivables"),
    ("routes-analyst", "Probes transfer-route and credit-line outages.", "routes"),
    ("outflows-analyst", "Probes surprise outflows.", "outflows"),
]


def deepagents_available() -> bool:
    try:
        import deepagents  # noqa: F401
        import langchain_anthropic  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _run_agent(ctx: ToolContext) -> dict[str, Any]:
    """Real orchestrator. Returns {"text": final summary, "llm_calls": n}. Raises on any problem."""
    from deepagents import create_deep_agent
    from langchain_anthropic import ChatAnthropic
    from langchain_core.tools import StructuredTool

    model_name = os.getenv("CASHGUARD_AGENT_MODEL", DEFAULT_AGENT_MODEL)
    model = ChatAnthropic(model=model_name, max_tokens=1500, timeout=30, max_retries=0)
    tools = [StructuredTool.from_function(f, name=f.__name__, description=(f.__doc__ or f.__name__))
             for f in (ctx.list_shocks, ctx.get_history, ctx.search_shocks, ctx.simulate, ctx.optimize)]
    subagents = [{"name": n, "description": d,
                  "system_prompt": f"You are the {n}. Use search_shocks with focus='{focus}' (budget <= 30), optionally "
                                   "simulate/optimize to follow up, then report the findings exactly as the tools "
                                   "return them. Never invent numbers.",
                  "tools": [t for t in tools if t.name in ("search_shocks", "simulate", "optimize", "get_history")]}
                 for n, d, focus in SUBAGENTS]
    agent = create_deep_agent(model=model, tools=tools, system_prompt=SYSTEM_PROMPT, subagents=subagents)
    result = agent.invoke({"messages": [{"role": "user", "content": "Run the stress search for this company."}]},
                          config={"recursion_limit": 40})
    msgs = result.get("messages", [])
    llm_calls = sum(1 for m in msgs if getattr(m, "type", "") == "ai")
    text = ""
    for m in reversed(msgs):
        if getattr(m, "type", "") == "ai":
            c = m.content
            text = c if isinstance(c, str) else "".join(b.get("text", "") for b in c if isinstance(b, dict))
            if text.strip():
                break
    return {"text": text.strip(), "llm_calls": llm_calls}


# `agent_runner(ctx) -> {"text", "llm_calls"}` is injectable so tests can run a fake agent offline.
AgentRunner = Callable[[ToolContext], dict[str, Any]]


def explore(store: Store, *, seed: int, agent_runner: AgentRunner | None = None,
            max_calls: int = MAX_TOOL_CALLS, max_seconds: float = MAX_SECONDS) -> dict[str, Any]:
    """Run the agent. Returns {"agent": "deepagent"|"fallback", "reason", "narrative", "llm_calls", "tool_calls",
    "agent_ms"}. Never raises."""
    started = time.perf_counter()
    info: dict[str, Any] = {"agent": "fallback", "reason": "", "narrative": [], "llm_calls": 0, "tool_calls": 0}
    ctx: ToolContext | None = None
    try:
        if agent_runner is None:
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise AgentUnavailable("ANTHROPIC_API_KEY not set")
            if not deepagents_available():
                raise AgentUnavailable("deepagents / langchain-anthropic not installed")
            agent_runner = _run_agent
        ctx = ToolContext(store, seed=seed, max_calls=max_calls, max_seconds=max_seconds)
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cashguard-agent")
        fut = pool.submit(agent_runner, ctx)
        try:
            out = fut.result(timeout=max_seconds)
        except FutureTimeout:
            ctx.cancelled = True
            raise AgentUnavailable(f"agent timed out after {max_seconds:.0f}s") from None
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        narrative = [dict(s) for s in ctx.steps]
        if out.get("text"):
            narrative.append({"step": len(narrative) + 1, "tool": None, "args": {}, "result": {"summary": out["text"]}})
        info.update(agent="deepagent", narrative=narrative, llm_calls=int(out.get("llm_calls", 0)),
                    tool_calls=ctx.calls)
    except Exception as exc:  # noqa: BLE001 - any agent problem degrades to the deterministic search
        log.warning("deep agent unavailable (%s: %s); falling back", type(exc).__name__, str(exc)[:200])
        reason = str(exc) if isinstance(exc, AgentUnavailable) else f"{type(exc).__name__}: {str(exc)[:200]}"
        info.update(agent="fallback", reason=reason, narrative=[dict(s) for s in ctx.steps] if ctx else [],
                    tool_calls=ctx.calls if ctx else 0)
        if ctx:
            ctx.cancelled = True
    info["agent_ms"] = int((time.perf_counter() - started) * 1000)
    return info


def execute_deep(store: Store, run_id: str, *, agent_runner: AgentRunner | None = None) -> StressRun:
    """Agent exploration + the authoritative deterministic run, annotated with the agent trace."""
    run = store.get(StressRun, run_id)
    if run is None:
        raise LookupError(run_id)
    _save_run(store, run.model_copy(update={"status": "running"}), "stress_run.started")
    info = explore(store, seed=run.seed, agent_runner=agent_runner)
    extra = {"mode": "deepagent", "agent": info["agent"], "agent_reason": info["reason"],
             "narrative": info["narrative"],
             "agent_meta": {k: info[k] for k in ("llm_calls", "tool_calls", "agent_ms")}}
    return execute_run(store, run_id, extra=extra)


def run_deep_and_warm(store: Store, run_id: str) -> None:
    from .runs import warm_decisions
    run = execute_deep(store, run_id)
    if run.status == "completed":
        warm_decisions(store)
