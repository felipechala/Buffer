"""Buffer treasury agent: a deepagent that runs the payment run end to end.

Division of labour (the point of the design):
  * The LLM decides WHAT to investigate, which pay dates to try, which funding trade-off to propose and how to
    explain it. It plans with deepagents' `write_todos` and delegates to three subagents.
  * Every number comes from a deterministic tool (simulator, LP optimizer, stress set, risk search). A proposal can
    only be submitted from a tool-verified draft, so the agent cannot invent a plan.
  * Nothing moves until a finance lead approves. Execution is plain code (`approve_run`) through the existing
    gateway, which writes to the Nessie sandbox when enabled and reconciles after a re-sync.
  * A rejection reason is stored as a preference; its flags are enforced by the tools on every later run, and a
    revision run starts automatically.

`deepagents` / `langchain-anthropic` are imported lazily. Tests inject a fake `AgentRunner`.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from ..engine.models import Scenario, TwinState
from ..engine.optimizer import optimize_plan
from ..services.fixes import (
    Fix,
    HeldOut,
    ScheduleBillFix,
    describe_actions,
    is_clean,
    replay_under,
)
from ..services.stress import get_stress_set
from ..workflows.forecast import build_twin_from_store, day_index
from ..workflows.models import APBill, Approval, ARInvoice, Customer, Vendor, utcnow
from ..workflows.policy import evaluate_bill
from ..workflows.state import WorkflowError
from ..workflows.store import Store
from .runs import build_probes
from .search import Evaluator, assess_fixable, search
from .shocks import ShockBounds, ShockSpace

log = logging.getLogger("cashguard.ops")

DEFAULT_AGENT_MODEL = "claude-sonnet-5-5"
MAX_TOOL_CALLS = 45
MAX_SECONDS = 300.0
FOCI = ("all", "receivables", "routes", "outflows")
RunStatus = Literal["queued", "running", "awaiting_approval", "executed", "rejected", "failed", "superseded"]


# ------------------------------------------------------------------ models
class AgentStep(BaseModel):
    step: int
    agent: str                      # "buffer" (main) or a subagent name
    tool: str
    args: dict[str, Any] = {}
    result: dict[str, Any] = {}
    ms: int = 0


class ProposalBill(BaseModel):
    bill_id: str
    payee: str
    amount_cents: int
    pay_date: date
    due_date: date
    moved: bool                     # differs from the bill's current date


class Proposal(BaseModel):
    draft_id: str
    snapshot_id: str
    bills: list[ProposalBill]
    funding: list[dict[str, Any]]   # FixAction dumps (transfer / credit_draw)
    plan_actions: list[dict[str, Any]]
    cost_cents: int
    discounts_cents: int = 0        # early-payment discounts captured by the schedule
    net_benefit_cents: int = 0      # discounts - funding cost
    verified: bool
    stress_checks: list[dict[str, Any]]
    scenarios_checked: int
    avoid: list[str]
    rationale: str
    held_bills: list[dict[str, Any]] = []   # bills the agent decided not to schedule now, with reason


class AgentRun(BaseModel):
    id: str
    parent_id: str | None = None
    goal: str = "weekly_payment_run"
    trigger: list[str] = []         # why the run started (watcher findings, rejection reason, user click)
    alert_keys: list[str] = []      # watcher alerts this run already answers (dedupe)
    status: RunStatus = "queued"
    snapshot_id: str = ""
    started_at: datetime
    finished_at: datetime | None = None
    todos: list[dict[str, Any]] = []
    steps: list[AgentStep] = []
    summary: str = ""
    proposal: Proposal | None = None
    error: str = ""
    decided_by: str | None = None
    rejection_reason: str = ""
    execution: dict[str, Any] | None = None
    llm_calls: int = 0
    model: str = ""


class AgentPreference(BaseModel):
    id: str
    text: str
    flags: list[str] = []           # enforced by tools: no_credit, no_transfers
    created_by: str
    run_id: str
    created_at: datetime


def preference_flags(text: str, accounts: list[tuple[str, str]] = ()) -> list[str]:
    """Keyword flags the tools enforce. `accounts` = (id, name) pairs: naming an account protects it
    (no transfers may be sourced from it)."""
    t = text.lower()
    flags = []
    for acct_id, name in accounts:
        words = {acct_id.lower(), name.lower(), *[w for w in name.lower().split() if len(w) > 4]}
        if any(re.search(rf"\b{re.escape(w)}\b", t) for w in words):
            flags.append(f"protect:{acct_id}")
    if re.search(r"credit|line of credit|\bloc\b|borrow|draw", t):
        flags.append("no_credit")
    if re.search(r"transfer|sweep|move (cash|money)|between accounts", t):
        flags.append("no_transfers")
    return flags


# ------------------------------------------------------------------ live registry
# Running runs live in memory (polled every second by the UI); they are persisted at each state change.
_LIVE: dict[str, AgentRun] = {}
_LIVE_LOCK = threading.Lock()


def _persist(store: Store, run: AgentRun, action: str) -> None:
    with _LIVE_LOCK:
        _LIVE[run.id] = run
    store.save(run, actor="agent", action=action, correlation_id=run.id)


def get_run(store: Store, run_id: str) -> AgentRun | None:
    if run_id == "latest":
        runs = list_runs(store)
        return runs[0] if runs else None
    with _LIVE_LOCK:
        live = _LIVE.get(run_id)
    return live or store.get(AgentRun, run_id)


def list_runs(store: Store) -> list[AgentRun]:
    stored = {r.id: r for r in store.list(AgentRun)}
    with _LIVE_LOCK:
        stored.update({k: v for k, v in _LIVE.items() if k in stored})
    return sorted(stored.values(), key=lambda r: r.started_at, reverse=True)


def preferences(store: Store) -> list[AgentPreference]:
    return sorted(store.list(AgentPreference), key=lambda p: p.created_at)


def _usd(cents: int) -> float:
    return round(cents / 100, 2)


class AgentUnavailable(RuntimeError):
    pass


# ------------------------------------------------------------------ tools
class OpsContext:
    """Deterministic tools. Enforces call/time caps, records every step on the run."""

    def __init__(self, store: Store, run: AgentRun, *, max_calls: int = MAX_TOOL_CALLS,
                 max_seconds: float = MAX_SECONDS):
        self.store = store
        self.run = run
        self.cfg = store.get_config()
        self.base: TwinState = build_twin_from_store(store)
        self.stress = get_stress_set(self.base, self.cfg.risk_tolerance)
        self.prefs = preferences(store)
        self.pref_flags = sorted({f for p in self.prefs for f in p.flags})
        self.max_calls, self.deadline = max_calls, time.monotonic() + max_seconds
        self.calls = 0
        self.drafts: dict[str, dict[str, Any]] = {}
        self.proposal: Proposal | None = None
        self.cancelled = False
        self._lock = threading.Lock()
        self.vendor_objs = {v.id: v for v in store.list(Vendor)}
        self.vendors = {v.id: v.name for v in self.vendor_objs.values()}

    # ---- bookkeeping
    def _call(self, agent: str, tool: str, args: dict[str, Any], fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            if self.cancelled or time.monotonic() > self.deadline:
                raise AgentUnavailable("agent time budget exhausted")
            if self.calls >= self.max_calls:
                return {"error": f"tool-call budget of {self.max_calls} exhausted; submit your best verified "
                                 "draft or stop now"}
            self.calls += 1
            step = AgentStep(step=len(self.run.steps) + 1, agent=agent, tool=tool, args=args)
            self.run.steps.append(step)
        t0 = time.perf_counter()
        try:
            result = fn()
        except WorkflowError as exc:
            result = {"error": exc.message if hasattr(exc, "message") else str(exc)}
        except AgentUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - tool errors go back to the model, not up the stack
            log.exception("tool %s failed", tool)
            result = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        step.result = result
        step.ms = int((time.perf_counter() - t0) * 1000)
        return result

    def _bill(self, bill_id: str) -> APBill:
        bill = self.store.get(APBill, bill_id)
        if bill is None:
            raise WorkflowError("not_found", f"bill {bill_id} not found", 404)
        return bill

    def _nondeferrable(self, bill: APBill) -> bool:
        return bill.nondeferrable or bill.category in self.cfg.policy.nondeferrable_categories

    def _current_day(self, bill: APBill) -> int:
        d = bill.scheduled_date if bill.status == "scheduled" and bill.scheduled_date else bill.due_date
        return max(0, day_index(self.cfg, d))

    def _discount(self, bill: APBill) -> tuple[float, date | None]:
        v = self.vendor_objs.get(bill.vendor_id)
        if v is None or v.early_pay_discount_pct <= 0 or bill.status not in ("approved", "scheduled"):
            return 0.0, None
        return v.early_pay_discount_pct, bill.earliest_pay_date + timedelta(days=v.early_pay_days)

    def _discount_info(self, b: APBill) -> dict[str, Any] | None:
        pct, by = self._discount(b)
        if not pct or by is None:
            return None
        return {"pct": pct, "pay_by": by.isoformat(), "savings_usd": _usd(int(round(b.amount_cents * pct)))}

    def _discounts_for(self, days: dict[str, int]) -> tuple[int, list[dict[str, Any]]]:
        """Discounts captured if each bill is paid on days[bill] (else its current day)."""
        total, rows = 0, []
        for b in self.store.list(APBill):
            pct, by = self._discount(b)
            if not pct or by is None:
                continue
            day = days.get(b.id, self._current_day(b))
            got = self.cfg.as_of + timedelta(days=day) <= by
            cents = int(round(b.amount_cents * pct))
            total += cents if got else 0
            rows.append({"bill_id": b.id, "pay_by": by.isoformat(), "discount_usd": _usd(cents), "captured": got})
        return total, rows

    # ---- tool bodies
    def cash_position(self) -> dict[str, Any]:
        from ..engine.simulator import simulate
        base = simulate(self.base, Scenario())
        low: dict[str, tuple[int, int]] = {}
        buf = {a.id: a.minimum_buffer_cents for a in self.base.accounts}
        for r in base.ledger:
            h = r.closing_cents - buf[r.account_id]
            if r.account_id not in low or h < low[r.account_id][0]:
                low[r.account_id] = (h, r.day)
        as_of = self.base.as_of
        return {
            "company": self.base.company_name, "as_of": as_of.isoformat(), "horizon_days": self.base.horizon_days,
            "snapshot_id": self.base.snapshot_id,
            "accounts": [{"id": a.id, "name": a.name, "balance_usd": _usd(a.observed_balance_cents),
                          "buffer_usd": _usd(a.minimum_buffer_cents),
                          "lowest_headroom_usd": _usd(low[a.id][0]) if a.id in low else None,
                          "lowest_headroom_date": (as_of + timedelta(days=low[a.id][1])).isoformat()
                          if a.id in low else None} for a in self.base.accounts],
            "baseline_breach": base.metrics.breach,
            "baseline_worst_shortfall_usd": _usd(base.metrics.worst_shortfall_cents),
            "expected_receipts": [{"id": r.id, "from": r.counterparty, "account": r.account_id,
                                   "amount_usd": _usd(r.amount_cents),
                                   "expected_date": (as_of + timedelta(days=r.expected_day)).isoformat(),
                                   "delay_days_probability": {str(k): v for k, v in r.delay_distribution.items()}}
                                  for r in sorted(self.base.receipts, key=lambda r: r.expected_day)],
            "routes": [{"id": r.id, "from": r.source_id, "to": r.destination_id, "settlement_days": r.settlement_days,
                        "fee_rate": r.fee_rate} for r in self.base.routes],
            "credit_lines": [{"id": c.id, "account": c.account_id, "fee_rate": c.fee_rate,
                              "draw_delay_days": c.draw_delay_days} for c in self.base.credit_lines],
            "stress_set": {"id": self.stress.id, "source": self.stress.source,
                           "scenarios": [s.description or s.id for s in self.stress.scenarios]},
        }

    def open_bills(self) -> dict[str, Any]:
        rows = []
        for b in sorted(self.store.list(APBill), key=lambda b: (b.due_date, b.id)):
            if b.status in ("paid", "rejected"):
                continue
            pol = evaluate_bill(self.cfg, self.store, b)
            nd = self._nondeferrable(b)
            movable = b.status in ("approved", "scheduled") and not nd
            rows.append({
                "bill_id": b.id, "payee": self.vendors.get(b.vendor_id, b.vendor_id), "category": b.category,
                "amount_usd": _usd(b.amount_cents), "status": b.status, "account": b.source_account_id,
                "earliest_pay_date": b.earliest_pay_date.isoformat(), "due_date": b.due_date.isoformat(),
                "scheduled_date": b.scheduled_date.isoformat() if b.scheduled_date else None,
                "nondeferrable": nd, "movable": movable,
                "needs_scheduling": b.status == "approved",
                "note": ("draft: not approved, cannot be scheduled by the agent" if b.status in ("draft", "pending")
                         else "fixed date (payroll/tax or nondeferrable)" if nd else ""),
                "early_pay_discount": self._discount_info(b),
                "policy": pol.reasons})
        return {"as_of": self.cfg.as_of.isoformat(), "bills": rows}

    def customer_history(self, customer: str) -> dict[str, Any]:
        q = customer.lower()
        custs = [c for c in self.store.list(Customer) if q in c.id.lower() or q in c.name.lower()]
        if not custs:
            return {"error": f"no customer matches {customer!r}",
                    "customers": [c.name for c in self.store.list(Customer)]}
        out = []
        for c in custs:
            invs = [i for i in self.store.list(ARInvoice) if i.customer_id == c.id]
            paid = [i for i in invs if i.status == "paid"]
            open_ = [i for i in invs if i.status in ("issued", "overdue")]
            out.append({
                "customer": c.name, "payment_terms_days": c.payment_terms_days,
                "delay_distribution": {str(k): v for k, v in c.delay_distribution.items()},
                "paid_invoices": len(paid),
                "open_invoices": [{"id": i.id, "amount_usd": _usd(i.amount_cents), "status": i.status,
                                   "expected_date": i.expected_receipt_date.isoformat()} for i in open_]})
        return {"customers": out}

    def risk_search(self, focus: str, budget: int) -> dict[str, Any]:
        focus = focus if focus in FOCI else "all"
        budget = max(5, min(int(budget), 40))
        from .deep import _focus_bounds
        bounds = _focus_bounds(ShockBounds.from_config(self.cfg.shock_bounds), focus)
        probes = build_probes(self.store, self.base)
        res = search(probes, bounds, budget=budget, seed=0)
        ev = Evaluator(probes, ShockSpace.from_twin(self.base, bounds))
        by_id = {p.id: p.twin for p in probes}
        findings = []
        for e in [x for x in res.elites if x.breach and x.plausibility > 0][:3]:
            scen = ev.scenario(e.shock, e.k)
            fx = assess_fixable(by_id[e.probe], scen)
            findings.append({"label": scen.description, "worst_shortfall_usd": _usd(e.worst_shortfall_cents),
                             "plausibility": round(e.plausibility, 6), "fixable_with_funding": fx.fixable})
        return {"focus": focus, "evaluations": res.evaluations, "findings": findings,
                "note": "findings are shocks that break the CURRENT plan; check_payment_run checks your schedule "
                        f"against the {len(self.stress.scenarios)}-scenario stress set"}

    def check_run(self, schedule: dict[str, str], avoid: list[str]) -> dict[str, Any]:
        days: dict[str, int] = {}
        errors: list[str] = []
        for bill_id, iso in (schedule or {}).items():
            try:
                bill = self._bill(bill_id)
                d = date.fromisoformat(str(iso)[:10])
            except WorkflowError as exc:
                errors.append(str(exc)); continue
            except ValueError:
                errors.append(f"{bill_id}: bad date {iso!r} (use YYYY-MM-DD)"); continue
            if bill.status not in ("approved", "scheduled"):
                errors.append(f"{bill_id} is {bill.status}; only approved bills can be scheduled")
            elif self._nondeferrable(bill) and day_index(self.cfg, d) != self._current_day(bill):
                errors.append(f"{bill_id} is nondeferrable ({bill.category}); its date cannot move")
            elif not (max(bill.earliest_pay_date, self.cfg.as_of) <= d <= bill.due_date):
                errors.append(f"{bill_id}: {d} is outside its legal window "
                              f"{max(bill.earliest_pay_date, self.cfg.as_of)}..{bill.due_date}")
            else:
                days[bill_id] = day_index(self.cfg, d)
        if errors:
            return {"error": "schedule rejected", "problems": errors}

        avoid_set = {a for a in (avoid or []) if a in ("credit", "transfers")}
        avoid_set |= {"credit" for f in self.pref_flags if f == "no_credit"}
        avoid_set |= {"transfers" for f in self.pref_flags if f == "no_transfers"}
        protected = sorted(f.split(":", 1)[1] for f in self.pref_flags if f.startswith("protect:"))
        twin = build_twin_from_store(self.store, bill_pay_days=days)
        stress = list(self.stress.scenarios)
        unfunded = replay_under(twin, [], stress)
        actions: list = []
        status = "no_funding_needed"
        if not is_clean(unfunded):
            primary = Scenario(id="agent-avoid",
                               disabled_credit_lines=[c.id for c in twin.credit_lines] if "credit" in avoid_set else [],
                               disabled_routes=[r.id for r in twin.routes
                                                if "transfers" in avoid_set or r.source_id in protected])
            plan = optimize_plan(twin, primary, stress=stress)
            if plan.status != "feasible":
                expl = [e for v in plan.violations for e in v.explanations][:4]
                return {"verified": False, "status": plan.status,
                        "unfunded_worst_shortfall_usd": _usd(max(c.worst_shortfall_cents for c in unfunded)),
                        "avoid": sorted(avoid_set), "protected_accounts": protected, "why": expl,
                        "hint": "move deferrable bills later (within their windows) or relax `avoid`"}
            actions = list(plan.actions)
            status = "funded"
        checks = replay_under(twin, actions, stress)
        fix_actions = describe_actions(twin, actions)
        cost = sum(getattr(a, "fee_cents", 0) for a in fix_actions)
        verified = is_clean(checks)
        disc, disc_rows = self._discounts_for(days)
        draft_id = f"draft-{len(self.drafts) + 1}"
        self.drafts[draft_id] = {"days": days, "actions": actions, "fix_actions": fix_actions, "cost": cost,
                                 "discounts": disc,
                                 "checks": checks, "verified": verified, "avoid": sorted(avoid_set),
                                 "snapshot_id": self.base.snapshot_id}
        return {
            "draft_id": draft_id, "verified": verified, "status": status, "avoid": sorted(avoid_set),
            "protected_accounts": protected,
            "scenarios_checked": len(checks),
            "unfunded": {"breaches": sum(c.breach for c in unfunded),
                         "worst_shortfall_usd": _usd(max(c.worst_shortfall_cents for c in unfunded))},
            "funding_actions": [_describe_fix_action(a) for a in fix_actions],
            "funding_cost_usd": _usd(cost),
            "discounts_captured_usd": _usd(disc), "net_benefit_usd": _usd(disc - cost),
            "discounts": disc_rows,
            "min_headroom_usd": _usd(min(c.min_headroom_cents for c in checks)),
            "schedule": {b: (self.cfg.as_of + timedelta(days=d)).isoformat() for b, d in days.items()},
        }

    def submit(self, draft_id: str, rationale: str, held: list[dict[str, Any]] | None) -> dict[str, Any]:
        d = self.drafts.get(draft_id)
        if d is None:
            return {"error": f"unknown draft {draft_id}; call check_payment_run first"}
        if not d["verified"]:
            return {"error": f"{draft_id} is not verified against every scenario; it cannot be proposed"}
        bills = []
        for bill_id, day in d["days"].items():
            b = self._bill(bill_id)
            bills.append(ProposalBill(bill_id=b.id, payee=self.vendors.get(b.vendor_id, b.vendor_id),
                                      amount_cents=b.amount_cents, pay_date=self.cfg.as_of + timedelta(days=day),
                                      due_date=b.due_date, moved=day != self._current_day(b)))
        self.proposal = Proposal(
            draft_id=draft_id, snapshot_id=d["snapshot_id"], bills=sorted(bills, key=lambda b: (b.pay_date, b.bill_id)),
            funding=[a.model_dump(mode="json") for a in d["fix_actions"]],
            plan_actions=[a.model_dump(mode="json") for a in d["actions"]], cost_cents=d["cost"],
            discounts_cents=d["discounts"], net_benefit_cents=d["discounts"] - d["cost"],
            verified=True, stress_checks=[c.model_dump(mode="json") for c in d["checks"]],
            scenarios_checked=len(d["checks"]), avoid=d["avoid"], rationale=rationale.strip(),
            held_bills=[{"bill_id": str(h.get("bill_id", "")), "reason": str(h.get("reason", ""))}
                        for h in (held or []) if isinstance(h, dict)])
        return {"submitted": draft_id, "status": "awaiting_approval",
                "next": "Stop now. A finance lead will approve or reject; do not call more tools."}

    # ---- LangChain-facing tools (one set per agent so every step is labelled with who called it)
    def tools_for(self, agent: str, names: tuple[str, ...] | None = None) -> list:
        from langchain_core.tools import StructuredTool

        ctx = self

        def get_cash_position() -> dict:
            """Balances, buffers, lowest projected headroom per account, expected receipts with delay odds,
            transfer routes, credit lines and the stress-set scenarios. Call this first."""
            return ctx._call(agent, "get_cash_position", {}, ctx.cash_position)

        def list_open_bills() -> dict:
            """Every unpaid bill: amount, status, legal pay window, whether it is movable, policy notes.
            Bills with needs_scheduling=true are the ones the payment run must schedule."""
            return ctx._call(agent, "list_open_bills", {}, ctx.open_bills)

        def get_customer_history(customer: str) -> dict:
            """Payment behaviour of a customer (name or id): terms, delay distribution, open invoices."""
            return ctx._call(agent, "get_customer_history", {"customer": customer},
                             lambda: ctx.customer_history(customer))

        def run_risk_search(focus: str = "receivables", budget: int = 25) -> dict:
            """Adaptive search for plausible shocks that break the current cash plan.
            focus: all | receivables | routes | outflows. budget: evaluations, 5-40."""
            return ctx._call(agent, "run_risk_search", {"focus": focus, "budget": budget},
                             lambda: ctx.risk_search(focus, budget))

        def check_payment_run(schedule: dict[str, str], avoid: list[str] | None = None) -> dict:
            """Verify a candidate payment run. schedule maps bill_id -> pay date (YYYY-MM-DD) for every bill you
            want to schedule; unlisted bills keep their current date. avoid may contain 'credit' and/or
            'transfers'. Simulates baseline + every stress scenario, finds the cheapest robust funding plan
            and replays it in exact cents. Returns a draft_id; only verified drafts can be proposed.
            Learned preferences are enforced automatically."""
            return ctx._call(agent, "check_payment_run", {"schedule": schedule, "avoid": avoid or []},
                             lambda: ctx.check_run(schedule, avoid or []))

        def predict_collections() -> dict:
            """Learned payment-timing model: per open invoice, P(late) and P(3+ days late) next to the per-customer
            assumption the engine uses, plus the model card (holdout log-loss vs that assumption). Advisory:
            use it to say which receipts deserve attention; the engine's checks stay authoritative."""
            def body() -> dict:
                from ..ml.payment_timing import get_model
                m = get_model(ctx.store)
                card = m.card
                return {"model_card": {"holdout_log_loss_model": card.holdout_log_loss_model,
                                       "holdout_log_loss_assumption": card.holdout_log_loss_histogram,
                                       "improvement_pct": card.improvement_pct,
                                       "rows": card.trained_on_real + card.trained_on_synthetic,
                                       "synthetic_rows": card.trained_on_synthetic, "note": card.note},
                        "invoices": [{"invoice_id": p.invoice_id, "customer": p.customer,
                                      "amount_usd": _usd(p.amount_cents), "due_date": p.due_date.isoformat(),
                                      "p_late_model": p.p_late, "p_late_assumed": round(1 - p.assumed_distribution[0], 4),
                                      "p_3plus_days_late_model": p.p_late_3plus}
                                     for p in m.predict(ctx.store)]}
            return ctx._call(agent, "predict_collections", {}, body)

        def read_preferences() -> dict:
            """Preferences the finance lead taught you by rejecting earlier proposals. Always respect them."""
            return ctx._call(agent, "read_preferences", {}, lambda: {
                "preferences": [{"text": p.text, "enforced_flags": p.flags} for p in ctx.prefs]})

        def submit_proposal(draft_id: str, rationale: str, held_bills: list[dict] | None = None) -> dict:
            """Submit a verified draft to the finance lead for approval. rationale: 3-5 sentences quoting tool
            numbers (cost, shortfall avoided, what moved and why). held_bills: optional list of
            {bill_id, reason} for bills you deliberately left unscheduled (e.g. drafts needing approval)."""
            return ctx._call(agent, "submit_proposal", {"draft_id": draft_id},
                             lambda: ctx.submit(draft_id, rationale, held_bills))

        fns = [get_cash_position, list_open_bills, get_customer_history, predict_collections, run_risk_search,
               check_payment_run, read_preferences, submit_proposal]
        if names is not None:
            fns = [f for f in fns if f.__name__ in names]
        return [StructuredTool.from_function(f, name=f.__name__, description=f.__doc__) for f in fns]


def _describe_fix_action(a) -> dict[str, Any]:
    d = a.model_dump(mode="json")
    if d["kind"] == "transfer":
        return {"kind": "transfer", "amount_usd": _usd(d["amount_cents"]), "from": d["from_account"],
                "to": d["to_account"], "route": d["route_id"], "date": d["dispatch_date"], "fee_usd": _usd(d["fee_cents"])}
    return {"kind": "credit_draw", "amount_usd": _usd(d["amount_cents"]), "line": d["line_id"],
            "account": d["account"], "date": d["date"], "fee_usd": _usd(d["fee_cents"])}


# ------------------------------------------------------------------ the deep agent
SYSTEM_PROMPT = """You are Buffer, an autonomous treasury agent for a small company. Your job: plan this week's \
payment run. Every bill must be paid on a legal date and every account must stay above its minimum buffer, \
even under the stress scenarios. Within that, maximise net benefit = early-payment discounts captured minus \
funding cost (paying early to win a discount is only worth it if the funding it needs costs less). You then \
submit ONE verified proposal to the finance lead and stop.

How to work:
1. Your FIRST call must be write_todos with 4-6 todos for this run. Mark each todo in_progress / completed with
   write_todos as you go (the finance lead watches this list live), and complete them all before you finish.
2. Call read_preferences, get_cash_position and list_open_bills yourself.
3. Delegate investigation with the task tool:
   - cash-analyst: which receivables are at risk and which shocks break the plan.
   - payables-planner: search pay dates for the bills that need scheduling (try several schedules).
   - funding-planner: compare funding trade-offs (transfers vs credit) for the best schedule.
4. Pick the verified draft with the highest net_benefit_usd and call submit_proposal.

Rules:
- Only bills with needs_scheduling=true are scheduled. Drafts cannot be scheduled: list them in held_bills.
- Payroll, tax and other nondeferrable bills never move.
- Every number you write must be copied from a tool result. Never compute or invent figures.
- Learned preferences are rules, not suggestions.
- You have a limited tool budget: be decisive."""

SUBAGENTS = [
    ("cash-analyst",
     "Investigates cash risk: receivables at risk (customer history) and shocks that break the plan (risk search). "
     "Give it a concrete question.",
     "You are the cash analyst. Call predict_collections (learned payment-timing model) first, then "
     "get_customer_history on the customers with the largest open invoices and "
     "run_risk_search (focus receivables, then routes if useful; budget <= 25). Report the top risks with the "
     "exact dollar figures and labels from the tools, in under 120 words.",
     ("get_cash_position", "predict_collections", "get_customer_history", "run_risk_search")),
    ("payables-planner",
     "Searches pay dates for the bills that need scheduling and returns the best verified draft ids. Give it the "
     "bill ids, their legal windows and any preferences.",
     "You are the payables planner. Try 3-5 schedules with check_payment_run: all on due date (baseline); every "
     "discount captured (pay discount bills by their pay_by date); subsets of the discounts. Respect legal "
     "windows; never move nondeferrable bills. Report each draft_id with verified, discounts_captured_usd, "
     "funding_cost_usd, net_benefit_usd and unfunded worst shortfall, quoting the tool output exactly, and "
     "recommend the one with the highest net benefit.",
     ("list_open_bills", "check_payment_run", "read_preferences")),
    ("funding-planner",
     "Compares funding options (transfers vs credit draw) for a given schedule. Give it the schedule.",
     "You are the funding planner. For the schedule you are given, call check_payment_run with avoid=[] and then "
     "with avoid=['credit'] and avoid=['transfers'] where relevant. Report each draft_id, verified flag, funding "
     "actions, funding_cost_usd and net_benefit_usd exactly as returned, and recommend the best verified one.",
     ("check_payment_run", "read_preferences")),
]

AgentRunner = Callable[[OpsContext, str], dict[str, Any]]


def deepagents_available() -> bool:
    try:
        import deepagents  # noqa: F401
        import langchain_anthropic  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _text_of(msg) -> str:
    c = getattr(msg, "content", "")
    return c if isinstance(c, str) else "".join(b.get("text", "") for b in c if isinstance(b, dict))


def _run_deep_agent(ctx: OpsContext, prompt: str) -> dict[str, Any]:
    from deepagents import create_deep_agent
    from langchain_anthropic import ChatAnthropic

    model_name = os.getenv("CASHGUARD_AGENT_MODEL", DEFAULT_AGENT_MODEL)
    ctx.run.model = model_name
    model = ChatAnthropic(model=model_name, max_tokens=4000, timeout=90, max_retries=1)
    subagents = [{"name": n, "description": d, "system_prompt": sp, "tools": ctx.tools_for(n, tools)}
                 for n, d, sp, tools in SUBAGENTS]
    from langchain.agents.middleware import TodoListMiddleware
    agent = create_deep_agent(model=model, tools=ctx.tools_for("buffer"), system_prompt=SYSTEM_PROMPT,
                              subagents=subagents, middleware=[TodoListMiddleware()])
    state: dict[str, Any] = {}
    for state in agent.stream({"messages": [{"role": "user", "content": prompt}]},
                              config={"recursion_limit": 80}, stream_mode="values"):
        if ctx.cancelled:
            break
        todos = state.get("todos")
        if todos:
            ctx.run.todos = [dict(t) for t in todos]
        msgs = state.get("messages", [])
        ctx.run.llm_calls = sum(1 for m in msgs if getattr(m, "type", "") == "ai")
        for m in msgs[-1:]:
            for tc in getattr(m, "tool_calls", None) or []:
                if tc.get("name") == "task":
                    a = tc.get("args", {})
                    ctx.run.steps.append(AgentStep(step=len(ctx.run.steps) + 1, agent="buffer", tool="task",
                                                   args={"subagent": a.get("subagent_type", ""),
                                                         "description": str(a.get("description", ""))[:400]}))
        if ctx.proposal is not None and msgs and getattr(msgs[-1], "type", "") == "tool":
            pass  # let the model write its closing message
    msgs = state.get("messages", [])
    text = next((_text_of(m) for m in reversed(msgs) if getattr(m, "type", "") == "ai" and _text_of(m).strip()), "")
    return {"text": text.strip()}


def build_prompt(store: Store, run: AgentRun) -> str:
    cfg = store.get_config()
    lines = [f"Today is {cfg.as_of.isoformat()} for {cfg.name}. Plan the payment run for the next "
             f"{cfg.horizon_days} days and submit one verified proposal."]
    if run.trigger:
        lines.append("You were started because:\n" + "\n".join(f"- {t}" for t in run.trigger))
    if run.parent_id:
        parent = store.get(AgentRun, run.parent_id)
        if parent and parent.proposal:
            p = parent.proposal
            lines.append(f"Your previous proposal ({parent.id}) was REJECTED by the finance lead with this reason: "
                         f"\"{parent.rejection_reason}\". It scheduled "
                         + ", ".join(f"{b.bill_id} on {b.pay_date}" for b in p.bills)
                         + f" with funding cost ${p.cost_cents / 100:,.2f}, discounts ${p.discounts_cents / 100:,.2f}"
                         + " and funding actions "
                         + (", ".join(f"{a['kind']} ${a['amount_cents'] / 100:,.0f}" for a in p.funding) or "none")
                         + ". Produce a different plan that addresses the reason.")
    return "\n\n".join(lines)


# ------------------------------------------------------------------ lifecycle
def start_run(store: Store, *, trigger: list[str] | None = None, parent_id: str | None = None,
              goal: str = "weekly_payment_run") -> tuple[AgentRun, bool]:
    """Returns (run, needs_execution). Only one run may be active at a time."""
    active = [r for r in list_runs(store) if r.status in ("queued", "running")]
    if active:
        return active[0], False
    try:
        keys = [a.key for a in scan(store)]
    except Exception:  # noqa: BLE001 - dedupe is best-effort
        keys = []
    run = AgentRun(id=f"agent-{uuid.uuid4().hex[:8]}", parent_id=parent_id, goal=goal, trigger=trigger or [],
                   snapshot_id=build_twin_from_store(store).snapshot_id, started_at=utcnow(), alert_keys=keys)
    _persist(store, run, "agent.run.queued")
    return run, True


def execute_run(store: Store, run_id: str, *, agent_runner: AgentRunner | None = None,
                max_calls: int = MAX_TOOL_CALLS, max_seconds: float = MAX_SECONDS) -> AgentRun:
    run = get_run(store, run_id)
    if run is None:
        raise LookupError(run_id)
    run = run.model_copy(update={"status": "running"})
    _persist(store, run, "agent.run.started")
    ctx: OpsContext | None = None
    try:
        if agent_runner is None:
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise AgentUnavailable("ANTHROPIC_API_KEY not set")
            if not deepagents_available():
                raise AgentUnavailable("deepagents / langchain-anthropic not installed")
            agent_runner = _run_deep_agent
        ctx = OpsContext(store, run, max_calls=max_calls, max_seconds=max_seconds)
        with _LIVE_LOCK:
            _LIVE[run.id] = run
        out = agent_runner(ctx, build_prompt(store, run))
        run.summary = out.get("text", "")
        if ctx.proposal is None:
            run.status = "failed"
            run.error = "the agent finished without submitting a verified proposal"
        else:
            run.proposal = ctx.proposal
            run.status = "awaiting_approval"
    except Exception as exc:  # noqa: BLE001
        log.warning("agent run %s failed: %s", run.id, exc)
        if ctx:
            ctx.cancelled = True
            if ctx.proposal is not None:           # a verified proposal survives a late failure
                run.proposal = ctx.proposal
        run.status = "awaiting_approval" if run.proposal else "failed"
        run.error = str(exc) if isinstance(exc, AgentUnavailable) else f"{type(exc).__name__}: {str(exc)[:300]}"
    run.finished_at = utcnow()
    if run.status == "awaiting_approval":          # only the newest proposal can be approved
        for old in list_runs(store):
            if old.id != run.id and old.status == "awaiting_approval":
                _persist(store, old.model_copy(update={"status": "superseded"}), "agent.run.superseded")
    _persist(store, run, f"agent.run.{run.status}")
    return run


def approve_run(store: Store, run_id: str, *, approved_by: str, connector=None, resync=None) -> AgentRun:
    from ..actions.gateway import execute_fix
    run = get_run(store, run_id)
    if run is None:
        raise WorkflowError("not_found", f"agent run {run_id} not found", 404)
    if run.status == "executed":
        return run                                   # idempotent
    if run.status != "awaiting_approval" or run.proposal is None:
        raise WorkflowError("not_actionable", f"run is {run.status}; only proposals awaiting approval", 409)
    cfg = store.get_config()
    user = cfg.user(approved_by)
    if user is None or user.role != "finance_lead":
        raise WorkflowError("forbidden", "approved_by must be a finance lead of this company", 403)
    p = run.proposal
    if p.snapshot_id != build_twin_from_store(store).snapshot_id:
        raise WorkflowError("stale_proposal", "financial state changed since this proposal was computed; "
                            "start a new agent run", 409)
    from ..engine.models import CreditDrawAction, TransferAction
    from ..services.fixes import CreditDrawFix, TransferFix
    plan_actions = [TransferAction.model_validate(a) if a.get("kind") == "transfer"
                    else CreditDrawAction.model_validate(a) for a in p.plan_actions]
    funding = [TransferFix.model_validate(a) if a.get("kind") == "transfer" else CreditDrawFix.model_validate(a)
               for a in p.funding]
    sched = [ScheduleBillFix(bill_id=b.bill_id, amount_cents=b.amount_cents,
                             pay_day=day_index(cfg, b.pay_date), pay_date=b.pay_date) for b in p.bills]
    first = min(p.bills, key=lambda b: b.pay_date) if p.bills else None
    fix = Fix(id=f"agentfix-{run.id}", bill_id=first.bill_id if first else "", snapshot_id=p.snapshot_id,
              run_id=run.id, kind="reschedule+funding" if funding else "reschedule",
              summary=f"Payment run: {len(p.bills)} bills, {len(funding)} funding actions",
              pay_day=day_index(cfg, first.pay_date) if first else 0,
              pay_date=first.pay_date if first else cfg.as_of, delay_days=0, cost_cents=p.cost_cents,
              actions=[*sched, *funding], verified=True, held_out=HeldOut(breach_rate=0.0, paths=0),
              stress_checks=p.stress_checks, plan_actions=plan_actions)
    store.save(fix, actor="agent", action="fix.proposed_by_agent", correlation_id=run.id)
    appr = Approval(id=f"appr-{fix.id}", subject="fix", subject_id=fix.id, approver_id=user.id, role=user.role,
                    decision="approved", timestamp=utcnow(), memo=f"agent run {run.id}")
    if store.get(Approval, appr.id) is None:
        store.save(appr, actor=user.id, action="approval.agent_run", correlation_id=run.id)
    ex = execute_fix(store, fix.id, idempotency_key=f"agent-{run.id}", connector=connector, resync=resync)
    run = run.model_copy(update={"status": "executed", "decided_by": user.id,
                                 "execution": ex.model_dump(mode="json")})
    _persist(store, run, "agent.run.executed")
    return run


def reject_run(store: Store, run_id: str, *, rejected_by: str, reason: str) -> tuple[AgentRun, AgentRun | None, bool]:
    """Returns (rejected run, revision run, revision needs execution)."""
    run = get_run(store, run_id)
    if run is None:
        raise WorkflowError("not_found", f"agent run {run_id} not found", 404)
    if run.status != "awaiting_approval":
        raise WorkflowError("not_actionable", f"run is {run.status}; only proposals awaiting approval", 409)
    user = store.get_config().user(rejected_by)
    if user is None or user.role != "finance_lead":
        raise WorkflowError("forbidden", "rejected_by must be a finance lead of this company", 403)
    reason = reason.strip() or "no reason given"
    accts = [(a.id, a.name) for a in store.get_config().accounts]
    pref = AgentPreference(id=f"pref-{run.id}", text=reason, flags=preference_flags(reason, accts), created_by=user.id,
                           run_id=run.id, created_at=utcnow())
    store.save(pref, actor=user.id, action="agent.preference.learned", correlation_id=run.id)
    run = run.model_copy(update={"status": "rejected", "decided_by": user.id, "rejection_reason": reason})
    _persist(store, run, "agent.run.rejected")
    rev, needs = start_run(store, parent_id=run.id, goal=run.goal,
                           trigger=[f"{user.name} rejected {run.id}: \"{reason}\""])
    return run, rev, needs


# ------------------------------------------------------------------ watcher
class Alert(BaseModel):
    key: str                        # stable id for dedupe
    kind: Literal["new_bill", "discount_expiring", "late_receivable", "buffer_breach"]
    message: str


def scan(store: Store) -> list[Alert]:
    """What changed that the payment run should react to. Pure read; deterministic."""
    from ..engine.simulator import simulate
    cfg = store.get_config()
    vendors = {v.id: v for v in store.list(Vendor)}
    customers = {c.id: c.name for c in store.list(Customer)}
    alerts: list[Alert] = []
    for b in store.list(APBill):
        if b.status != "approved":
            continue
        v = vendors.get(b.vendor_id)
        name = v.name if v else b.vendor_id
        alerts.append(Alert(key=f"bill:{b.id}:{b.amount_cents}", kind="new_bill",
                            message=f"Approved bill {b.id} from {name} (${b.amount_cents / 100:,.0f}) is not scheduled; "
                                    f"due {b.due_date}"))
        if v and v.early_pay_discount_pct > 0:
            by = b.earliest_pay_date + timedelta(days=v.early_pay_days)
            if cfg.as_of <= by <= cfg.as_of + timedelta(days=2):
                alerts.append(Alert(key=f"discount:{b.id}:{by}", kind="discount_expiring",
                                    message=f"{name} offers {v.early_pay_discount_pct:.1%} off {b.id} "
                                            f"(${b.amount_cents * v.early_pay_discount_pct / 100:,.0f}) only if paid by {by}"))
    for inv in store.list(ARInvoice):
        if inv.status in ("issued", "overdue") and inv.expected_receipt_date > inv.due_date:
            late = (inv.expected_receipt_date - inv.due_date).days
            alerts.append(Alert(key=f"late:{inv.id}:{inv.expected_receipt_date}", kind="late_receivable",
                                message=f"{customers.get(inv.customer_id, inv.customer_id)} invoice {inv.id} "
                                        f"(${inv.amount_cents / 100:,.0f}) is now expected {inv.expected_receipt_date}, "
                                        f"{late} day(s) after its due date"))
    twin = build_twin_from_store(store)
    m = simulate(twin, Scenario()).metrics
    if m.first_breach is not None:
        fb = m.first_breach
        alerts.append(Alert(key=f"breach:{twin.snapshot_id}", kind="buffer_breach",
                            message=f"{fb.account_id} is projected below its buffer on "
                                    f"{twin.as_of + timedelta(days=fb.day)} by ${fb.shortfall_cents / 100:,.0f}"))
    return alerts


def watch(store: Store) -> tuple[list[Alert], AgentRun | None, bool]:
    """Scan; start an agent run if there are alerts no earlier run has answered.
    Returns (new alerts, run, needs_execution)."""
    seen = {k for r in store.list(AgentRun) for k in r.alert_keys}
    alerts = scan(store)
    fresh = [a for a in alerts if a.key not in seen]
    if not fresh:
        return [], None, False
    order = {"buffer_breach": 0, "late_receivable": 1, "discount_expiring": 2, "new_bill": 3}
    fresh.sort(key=lambda a: order[a.kind])
    run, needs = start_run(store, trigger=[a.message for a in fresh[:6]], goal="respond_to_change")
    if needs:
        run = run.model_copy(update={"alert_keys": [a.key for a in alerts]})
        _persist(store, run, "agent.run.queued")
    return fresh, run, needs


def auto_enabled() -> bool:
    return os.getenv("CASHGUARD_AGENT_AUTO", "1") != "0"


DEMO_EVENTS = ("late_payment", "new_bill")


def inject_demo_event(store: Store, kind: str) -> str:
    """Demo helper: make the world change so the watcher has something to react to."""
    cfg = store.get_config()
    if kind == "late_payment":
        inv = store.get(ARInvoice, "INV-918") or next(
            (i for i in store.list(ARInvoice) if i.status == "issued"), None)
        if inv is None:
            raise WorkflowError("not_found", "no open invoice to delay", 404)
        new = inv.model_copy(update={"expected_receipt_date": max(inv.expected_receipt_date, inv.due_date)
                                     + timedelta(days=4)})
        store.save(new, actor="demo", action="invoice.expected_date_slipped", before=inv)
        return f"{inv.id} now expected {new.expected_receipt_date}"
    if kind == "new_bill":
        n = sum(1 for b in store.list(APBill) if b.id.startswith("BILL-NW-NEW"))
        bill = APBill(id=f"BILL-NW-NEW{n + 1}", vendor_id="northwind", amount_cents=3_500_000, category="supplier",
                      due_date=cfg.as_of + timedelta(days=11), earliest_pay_date=cfg.as_of,
                      source_account_id="operating", status="approved", created_by="u_omar",
                      memo="Northwind Supplies rush order (demo event)")
        store.save(bill, actor="u_omar", action="bill.created_and_approved")
        return f"{bill.id} created and approved"
    raise WorkflowError("invalid_request", f"kind must be one of {DEMO_EVENTS}", 422)
