"""Stress runs: persisted results of the adaptive search + the StressSet they produce for decisions.

Lifecycle: `queue_run` (status queued, saved) -> `execute_run` (running -> completed|failed; registers the
snapshot's `risk_search` StressSet) -> `warm_decisions` (fills the engine caches, writes nothing).
Run ids are deterministic in (snapshot, seed, budget), so a second sync of an unchanged snapshot reuses the run.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from ..engine.models import Scenario, TwinState
from ..engine.optimizer import MODEL_VERSION, optimize_plan
from ..engine.simulator import simulate
from ..services.stress import StressSet, default_stress_set, set_stress_set
from ..workflows.forecast import build_twin_from_store, day_index
from ..workflows.models import APBill, utcnow
from ..workflows.store import Store
from .search import Evaluator, Probe, assess_fixable, first_unfixable, search
from .shocks import ShockBounds, ShockSpace, ShockVector, plausibility

log = logging.getLogger("cashguard.stress")
DEFAULT_BUDGET = 120
MAX_SET_ELITES = 4


class EliteFinding(BaseModel):
    scenario: Scenario
    shock: ShockVector
    score: float
    plausibility: float
    probe: str                       # which payment posture it hurts most ("base" or "pay:<bill id>")
    worst_shortfall_cents: int
    first_breach: dict[str, Any] | None
    found_at: int
    reason: str
    fixable: bool
    plan_status: str
    infeasibility: list[str]


class StressRun(BaseModel):
    id: str
    snapshot_id: str
    status: str = "queued"           # queued | running | completed | failed
    source: str = "risk_search"
    seed: int = 0
    budget: int = DEFAULT_BUDGET
    created_at: datetime
    completed_at: datetime | None = None
    runtime_ms: int | None = None
    probes: list[str] = []
    elites: list[EliteFinding] = []
    trace: list[dict[str, Any]] = []
    adaptive: dict[str, Any] = {}
    random_baseline: dict[str, Any] = {}
    stress_set_id: str | None = None
    scenarios: list[Scenario] = []
    findings: list[dict[str, Any]] = []
    error: str | None = None
    mode: str = "deterministic"      # deterministic | deepagent
    agent: str = "none"              # none | deepagent | fallback
    agent_reason: str = ""
    narrative: list[dict[str, Any]] = []
    agent_meta: dict[str, Any] = {}


def audit_summary(run: StressRun) -> dict[str, Any]:
    """Audit events carry this, never the trace/elites (those live in the entity row)."""
    return {"run_id": run.id, "snapshot_id": run.snapshot_id, "status": run.status,
            "elites_count": len(run.elites), "budget": run.budget, "runtime_ms": run.runtime_ms}


def _save_run(store: Store, run: StressRun, action: str) -> None:
    store.save(run, actor="engine", action=action, correlation_id=run.id, audit_summary=audit_summary(run))


def run_id_for(snapshot_id: str, seed: int, budget: int, mode: str = "deterministic") -> str:
    suffix = "_deep" if mode == "deepagent" else ""
    return f"stress_{snapshot_id.removeprefix('snap_')[:10]}_s{seed}_b{budget}{suffix}"


def queue_run(store: Store, snapshot_id: str, *, seed: int = 0, budget: int = DEFAULT_BUDGET,
              force: bool = False, mode: str = "deterministic") -> tuple[StressRun, bool]:
    """Return (run, needs_execution). An existing queued/running/completed run is reused unless `force`."""
    rid = run_id_for(snapshot_id, seed, budget, mode)
    existing = store.get(StressRun, rid)
    if existing is not None and not force and existing.status != "failed":
        return existing, False
    run = StressRun(id=rid, snapshot_id=snapshot_id, seed=seed, budget=budget, created_at=utcnow(), mode=mode)
    _save_run(store, run, "stress_run.queued")
    return run, True


def latest_run(store: Store, snapshot_id: str) -> StressRun | None:
    runs = [r for r in store.list(StressRun) if r.snapshot_id == snapshot_id]
    return max(runs, key=lambda r: r.created_at, default=None)


def build_probes(store: Store, base: TwinState) -> list[Probe]:
    """Payment postures to stress: today's forecast, and each approved bill paid on its earliest date,
    keeping only postures that are healthy before any shock (a shock-independent breach is not a finding)."""
    cfg = store.get_config()
    probes = [Probe("base", base)]
    seen = {base.snapshot_id}
    for bill in sorted(store.list(APBill), key=lambda b: b.id):
        if bill.status != "approved":
            continue
        twin = build_twin_from_store(store, bill_pay_days={bill.id: max(0, day_index(cfg, bill.earliest_pay_date))})
        if twin.snapshot_id in seen or simulate(twin).metrics.breach:
            continue
        seen.add(twin.snapshot_id)
        probes.append(Probe(f"pay:{bill.id}", twin))
    return probes


def _jointly_fixable(probes: list[Probe], scenarios: list[Scenario]) -> bool:
    """One plan must survive the whole set (robust planning), on every posture the set can hurt."""
    for p in probes:
        if any(simulate(p.twin, s).metrics.breach for s in scenarios) and                 optimize_plan(p.twin, stress=scenarios).status != "feasible":
            return False
    return True


def _trace_row(e) -> dict[str, Any]:
    return {"k": e.k, "scenario_id": e.scenario_id, "reason": e.reason, "score": round(e.score, 6),
            "signal": round(e.signal, 4), "plausibility": round(e.plausibility, 6), "breach": e.breach,
            "probe": e.probe, "shock": e.shock.model_dump(mode="json")}


def execute_run(store: Store, run_id: str, *, shock_bounds: ShockBounds | None = None,
                extra: dict[str, Any] | None = None) -> StressRun:
    run = store.get(StressRun, run_id)
    if run is None:
        raise LookupError(run_id)
    started = time.perf_counter()
    _save_run(store, run.model_copy(update={"status": "running"}), "stress_run.started")
    try:
        cfg = store.get_config()
        base = build_twin_from_store(store)
        if base.snapshot_id != run.snapshot_id:
            raise RuntimeError("snapshot changed before the stress run started")
        bounds = shock_bounds or ShockBounds.from_config(cfg.shock_bounds)
        probes = build_probes(store, base)
        probe_twins = {p.id: p.twin for p in probes}
        ev = Evaluator(probes, ShockSpace.from_twin(base, bounds))
        adaptive = search(probes, bounds, budget=run.budget, seed=run.seed, evaluator=ev)
        baseline = search(probes, bounds, budget=run.budget, seed=run.seed, adaptive=False, evaluator=ev)

        findings: list[EliteFinding] = []
        for e in adaptive.elites:
            if not (e.breach and e.plausibility > 0):
                continue
            scen = ev.scenario(e.shock, e.k)
            fx = assess_fixable(probe_twins[e.probe], scen)
            findings.append(EliteFinding(
                scenario=scen, shock=e.shock, score=round(e.score, 6), plausibility=e.plausibility, probe=e.probe,
                worst_shortfall_cents=e.worst_shortfall_cents, first_breach=e.first_breach, found_at=e.k,
                reason=e.reason, fixable=fx.fixable, plan_status=fx.status, infeasibility=fx.report))
        scen_of = lambda e: ev.scenario(e.shock, e.k)  # noqa: E731
        adaptive_unfixable = first_unfixable(probe_twins, adaptive.trace, scen_of)
        random_unfixable = first_unfixable(probe_twins, baseline.trace, scen_of)

        tol = cfg.risk_tolerance
        defaults = default_stress_set(base, tol).scenarios
        kept_defaults = [s for s in defaults if s.id.startswith("adverse") or plausibility(base, s) >= tol / 5]
        # Only shocks some plan can survive on every payment posture constrain decisions; unfixable ones are
        # reported as findings (the danger list) but must not make every candidate fix fail verification.
        elite_scenarios: list[Scenario] = []
        for f in findings:
            if len(elite_scenarios) >= MAX_SET_ELITES:
                break
            if f.fixable and _jointly_fixable(probes, [*kept_defaults, *elite_scenarios, f.scenario]):
                elite_scenarios.append(f.scenario)
        scenarios = [*elite_scenarios, *kept_defaults]
        stress = StressSet(id=f"stress-search-{run.id}", snapshot_id=base.snapshot_id, source="risk_search",
                           scenarios=scenarios)
        set_stress_set(stress)

        out_findings = []
        for s in scenarios:
            worst = max(((p.id, simulate(p.twin, s).metrics) for p in probes),
                        key=lambda pm: (pm[1].breach, pm[1].worst_shortfall_cents))
            out_findings.append({"scenario_id": s.id, "label": s.description or s.id, "probe": worst[0],
                                 "breach": worst[1].breach, "worst_shortfall_cents": worst[1].worst_shortfall_cents,
                                 "first_breach": worst[1].first_breach.model_dump() if worst[1].first_breach else None})
        done = run.model_copy(update={
            "status": "completed", "completed_at": utcnow(), "probes": [p.id for p in probes], "elites": findings,
            "trace": [_trace_row(e) for e in adaptive.trace], "stress_set_id": stress.id, "scenarios": scenarios,
            "findings": out_findings,
            "adaptive": {"first_breach_found_at": adaptive.first_breach_found_at,
                         "first_unfixable_found_at": adaptive_unfixable, "unique_breaches": adaptive.unique_breaches,
                         "evaluations": adaptive.evaluations},
            "random_baseline": {"first_breach_found_at": baseline.first_breach_found_at,
                                "first_unfixable_found_at": random_unfixable,
                                "unique_breaches": baseline.unique_breaches, "evaluations": baseline.evaluations},
            "runtime_ms": int((time.perf_counter() - started) * 1000), **(extra or {})})
    except Exception as exc:  # noqa: BLE001 - a failed run must never take the app down
        log.exception("stress run %s failed", run_id)
        done = run.model_copy(update={"status": "failed", "error": f"{type(exc).__name__}: {exc}",
                                      "completed_at": utcnow(), **(extra or {})})
    _save_run(store, done, f"stress_run.{done.status}")
    return done


def warm_decisions(store: Store) -> int:
    """Pre-compute decisions for open bills so the first real call hits the engine caches.
    Writes nothing: no fixes, AnalysisRuns or audit events (decide(persist=False))."""
    from ..services.decision import ProposedPayment, SimulationRequest, decide
    cfg = store.get_config()
    n = 0
    for bill in store.list(APBill):
        if bill.status not in ("approved", "draft", "pending"):
            continue
        pay = bill.earliest_pay_date if bill.earliest_pay_date > cfg.as_of else None
        try:
            decide(store, SimulationRequest(proposed_action=ProposedPayment(bill_id=bill.id, pay_date=pay)),
                   persist=False)
            n += 1
        except Exception:  # noqa: BLE001
            log.exception("warm-up failed for %s", bill.id)
    return n


def run_and_warm(store: Store, run_id: str) -> None:
    """Background task: search, register the stress set, then warm the caches with the final set."""
    run = execute_run(store, run_id)
    if run.status == "completed":
        warm_decisions(store)
