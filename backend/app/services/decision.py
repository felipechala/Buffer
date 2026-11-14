"""The CashGuard decision: simulate a proposed payment, return allow / warn / block + fixes.

  allow  requested day needs no actions, no breach in baseline or stress set, held-out rate ok
  warn   something breaches (baseline or a stress scenario) and a verified fix exists,
         or the only safe plan carries residual risk (requires_review)
  block  the baseline breaches and no legal date / plan is feasible

The LLM never participates here. `explanation` is a deterministic template; the optional
explainer (services/explain.py) may replace it with grounded prose.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from ..engine.models import Scenario, SimulationResult, TwinState, Violation, fmt_usd
from ..engine.optimizer import MODEL_VERSION
from ..engine.simulator import simulate
from ..workflows.forecast import build_twin_from_store, day_index
from ..workflows.models import AnalysisRun, APBill, Vendor, utcnow
from ..workflows.state import WorkflowError
from ..workflows.store import Store
from .fixes import Fix, build_fixes
from .impact import ImpactRequest, ImpactResult, analyze_bill_impact
from .stress import StressSet, get_stress_set, plausibility

DecisionLevel = Literal["allow", "warn", "block"]
Priority = Literal["low", "normal", "high", "critical"]


# ------------------------------------------------------------------ request
class ProposedPayment(BaseModel):
    kind: Literal["payment"] = "payment"
    bill_id: str | None = None                  # a bill already in the store (synced from Nessie)
    amount_cents: int | None = Field(default=None, gt=0)
    from_account: str | None = None
    payee: str | None = None                    # vendor id or display name
    pay_date: date | None = None                # default: as_of (today)
    latest_date: date | None = None             # default: bill due date, else pay_date + 7
    priority: Priority = "normal"


class SimulationRequest(BaseModel):
    snapshot_id: str | None = None
    proposed_action: ProposedPayment | None = None
    scenario: Scenario | None = None            # judge override / counterfactual
    horizon_days: int | None = Field(default=None, ge=1, le=30)
    risk_tolerance: float | None = Field(default=None, gt=0, lt=0.5)
    scenario_count: int = Field(default=100, ge=10, le=2000)
    seed: int = 42


# ------------------------------------------------------------------ response
class ScenarioRef(BaseModel):
    id: str
    label: str
    plausibility: float


class Break(BaseModel):
    date: date
    day: int
    account: str
    obligation: str | None
    shortfall_cents: int
    scenario: ScenarioRef


class Provenance(BaseModel):
    snapshot_id: str
    input_digest: str
    engine_version: str
    scenario_seed: int
    stress_set_id: str
    stress_set_source: str
    scenarios_checked: int
    source: str
    assumptions: list[str]
    request_scenario: Scenario | None = None


class Decision(BaseModel):
    run_id: str
    decision: DecisionLevel
    safe: bool
    bill_id: str
    payee: str
    amount_cents: int
    requested_day: int
    requested_date: date
    breaks: list[Break]
    fixes: list[Fix]
    infeasibility: list[Violation] = []
    curves: dict[str, dict[str, list[int]]]      # "baseline" | "stressed" | fix id -> account -> closing
    constraint_report: list[str]
    explanation: str
    impact: ImpactResult
    provenance: Provenance
    latency_ms: int


# ------------------------------------------------------------------ helpers
def _digest(payload: dict) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _curve(result: SimulationResult) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for row in result.ledger:
        out.setdefault(row.account_id, []).append(row.closing_cents)
    return out


def _resolve_bill(store: Store, action: ProposedPayment, as_of: date) -> tuple[APBill, str, bool]:
    """Return (bill, payee display name, ephemeral?)."""
    if action.bill_id:
        bill = store.get(APBill, action.bill_id)
        if bill is None:
            raise WorkflowError("not_found", f"bill {action.bill_id} not found", 404)
        vendor = store.get(Vendor, bill.vendor_id)
        return bill, (vendor.name if vendor else bill.vendor_id), False
    if action.amount_cents is None or action.from_account is None:
        raise WorkflowError("invalid_request", "amount_cents and from_account are required "
                            "when bill_id is not given", 422)
    pay_date = action.pay_date or as_of
    latest = action.latest_date or pay_date + timedelta(days=7)
    if latest < pay_date:
        raise WorkflowError("invalid_request", "latest_date must be on or after pay_date", 422)
    vendor = None
    if action.payee:
        vendor = store.get(Vendor, action.payee) or next(
            (v for v in store.list(Vendor) if v.name.lower() == action.payee.lower()), None)
    payee = vendor.name if vendor else (action.payee or "payee")
    digest = _digest(action.model_dump(mode="json"))[-8:]
    bill = APBill(id=f"adhoc-{digest}", vendor_id=vendor.id if vendor else "adhoc",
                  amount_cents=action.amount_cents, category=vendor.category if vendor else "supplier",
                  due_date=latest, earliest_pay_date=pay_date, source_account_id=action.from_account,
                  status="approved", nondeferrable=action.priority == "critical",
                  memo=f"ad-hoc payment to {payee}")
    return bill, payee, True


def _breaks(result: SimulationResult, twin: TwinState, ref: ScenarioRef, limit: int = 3) -> list[Break]:
    out: list[Break] = []
    names = {o.id: (o.counterparty or o.id) for o in twin.obligations}
    breached_obl = {(o.account_id, o.due_day): o.obligation_id
                    for o in result.obligations if o.buffer_breached}
    for row in result.ledger:
        if row.shortfall_cents > 0:
            obl = breached_obl.get((row.account_id, row.day))
            out.append(Break(date=twin.as_of + timedelta(days=row.day), day=row.day,
                             account=row.account_id, obligation=names.get(obl) if obl else None,
                             shortfall_cents=row.shortfall_cents, scenario=ref))
    out.sort(key=lambda b: (b.day, -b.shortfall_cents))
    return out[:limit]


def _template_explanation(payee: str, amount: int, requested: date, breaks: list[Break],
                          fixes: list[Fix], decision: DecisionLevel) -> str:
    when = requested.strftime("%b %d")
    if decision == "allow":
        return (f"Paying {payee} {fmt_usd(amount)} on {when} keeps every account above its buffer "
                f"for the whole horizon, including under the stress scenarios checked.")
    if not breaks:
        return f"Paying {payee} {fmt_usd(amount)} on {when} needs review."
    b = breaks[0]
    what = f"{b.obligation} on {b.date.strftime('%b %d')}" if b.obligation else \
        f"the {b.account} account on {b.date.strftime('%b %d')}"
    cause = "" if b.scenario.id == "baseline" else f" if {b.scenario.label}"
    text = f"Paying {payee} {fmt_usd(amount)} on {when} leaves {what} short {fmt_usd(b.shortfall_cents)}{cause}."
    if fixes:
        f = fixes[0]
        cost = "at no cost" if f.cost_cents == 0 else f"for {fmt_usd(f.cost_cents)} in fees"
        text += f" Cheapest fix: {f.summary[0].lower() + f.summary[1:]} ({cost})."
    elif decision == "block":
        text += " No transfer or credit plan can cover it within the configured limits."
    return text


# ------------------------------------------------------------------ main entry
def decide(store: Store, req: SimulationRequest, *, stress_override: StressSet | None = None,
           persist: bool = True) -> Decision:
    """`persist=False` computes the decision without saving fixes, an AnalysisRun or audit events (cache warming)."""
    started = time.perf_counter()
    if req.proposed_action is None:
        raise WorkflowError("invalid_request", "proposed_action is required for a decision", 422)
    cfg = store.get_config()
    tol = req.risk_tolerance or cfg.risk_tolerance
    base_twin = build_twin_from_store(store)
    if req.snapshot_id and req.snapshot_id != base_twin.snapshot_id:
        raise WorkflowError("stale_snapshot", f"current snapshot is {base_twin.snapshot_id}; "
                            f"request named {req.snapshot_id}", 409)
    stress = stress_override or get_stress_set(base_twin, tol)
    scenarios = list(stress.scenarios)
    request_scenario = None
    if req.scenario is not None and req.scenario.model_dump(exclude={"id", "description"}) != Scenario().model_dump(
            exclude={"id", "description"}):
        request_scenario = req.scenario.model_copy(update={
            "id": req.scenario.id if req.scenario.id != Scenario().id else "request_scenario",
            "description": req.scenario.description or "scenario supplied with the request"})
        scenarios = [request_scenario, *scenarios]

    bill, payee, ephemeral = _resolve_bill(store, req.proposed_action, cfg.as_of)
    extra = [bill] if ephemeral else None
    requested_day = max(0, day_index(cfg, req.proposed_action.pay_date or
                                     (bill.earliest_pay_date if not ephemeral else cfg.as_of)))
    if req.proposed_action.pay_date is None and not ephemeral:
        requested_day = max(0, day_index(cfg, cfg.as_of))
    latest_day = min(day_index(cfg, bill.due_date), cfg.horizon_days - 1)
    if requested_day > latest_day:
        raise WorkflowError("invalid_request", "pay_date is after the bill's due date / horizon", 422)

    # Candidate days: the requested day, plus the first later legal day whose no-action
    # replay is clean under the baseline and every stress scenario (cheap pre-pass), plus
    # the latest legal day as the fallback for reschedule+funding.
    days = [requested_day]
    for d in range(requested_day + 1, latest_day + 1):
        t = build_twin_from_store(store, bill_pay_days={bill.id: d}, extra_bills=extra)
        if all(not simulate(t, s).metrics.breach for s in [Scenario(), *scenarios]):
            days.append(d)
            break
    if latest_day not in days and latest_day != requested_day:
        days.append(latest_day)

    impact = analyze_bill_impact(
        store, bill.id,
        ImpactRequest(candidate_payment_days=days, scenario_count=req.scenario_count,
                      risk_tolerance=tol, seed=req.seed),
        bill=bill if ephemeral else None, stress=scenarios)
    twins = {d: build_twin_from_store(store, bill_pay_days={bill.id: d}, extra_bills=extra) for d in days}
    twin0 = twins[requested_day]
    requested = next(o for o in impact.options if o.pay_day == requested_day)

    # ---- replay the requested day with no actions: baseline + every stress scenario
    baseline = simulate(twin0)
    stressed: list[tuple[Scenario, SimulationResult]] = [(s, simulate(twin0, s)) for s in scenarios]
    breaching = [(s, r) for s, r in stressed if r.metrics.breach]

    if baseline.metrics.breach:
        target_s, target_r = Scenario(), baseline
    elif request_scenario is not None and any(sc.id == request_scenario.id for sc, _ in breaching):
        target_s, target_r = next((sc, r) for sc, r in breaching if sc.id == request_scenario.id)
    elif breaching:
        target_s, target_r = max(breaching, key=lambda sr: plausibility(twin0, sr[0]))
    else:
        target_s, target_r = Scenario(), baseline
    ref = ScenarioRef(id=target_s.id, label=target_s.description or target_s.id,
                      plausibility=round(plausibility(twin0, target_s), 4) if target_s.id != "baseline" else 1.0)
    breaks = _breaks(target_r, twin0, ref) if target_r.metrics.breach else []

    needs_actions = bool(requested.nominal.actions) or requested.selected == "robust" \
        or requested.status != "feasible"
    safe_now = (not baseline.metrics.breach and not breaching and not needs_actions
                and requested.nominal.heldout_breach_rate <= tol)

    fixes: list[Fix] = []
    infeasibility: list[Violation] = []
    if safe_now:
        decision: DecisionLevel = "allow"
    else:
        fixes = build_fixes(payee=payee, bill_id=bill.id, amount_cents=bill.amount_cents,
                            requested_day=requested_day, options=impact.options, twins=twins,
                            stress=scenarios, risk_tolerance=tol, heldout_paths=req.scenario_count)
        if fixes:
            decision = "warn"
        elif baseline.metrics.breach:
            decision = "block"
            infeasibility = requested.robust.violations or requested.nominal.violations
        else:
            decision = "warn"
            infeasibility = requested.robust.violations or requested.nominal.violations

    # ---- curves
    curves: dict[str, dict[str, list[int]]] = {"baseline": _curve(baseline)}
    if target_s.id != "baseline":
        curves["stressed"] = _curve(target_r)
    for f in fixes:
        curves[f.id] = _curve(simulate(twins[f.pay_day], target_s, f.plan_actions))

    constraint_report: list[str] = []
    if fixes:
        constraint_report = list(dict.fromkeys(
            c for o in impact.options if o.pay_day == fixes[0].pay_day
            for c in (o.robust.binding_constraints if o.selected == "robust" else o.nominal.binding_constraints)))
    for v in infeasibility:
        constraint_report.extend(v.explanations)

    explanation = _template_explanation(payee, bill.amount_cents,
                                        cfg.as_of + timedelta(days=requested_day), breaks, fixes, decision)

    digest = _digest({"request": req.model_dump(mode="json"), "snapshot": base_twin.snapshot_id,
                      "stress": stress.id, "engine": MODEL_VERSION})
    run_id = "run_" + digest[-12:]
    result = Decision(
        run_id=run_id, decision=decision, safe=decision == "allow", bill_id=bill.id, payee=payee,
        amount_cents=bill.amount_cents, requested_day=requested_day,
        requested_date=cfg.as_of + timedelta(days=requested_day),
        breaks=breaks, fixes=fixes, infeasibility=infeasibility, curves=curves,
        constraint_report=constraint_report, explanation=explanation, impact=impact,
        provenance=Provenance(
            snapshot_id=base_twin.snapshot_id, input_digest=digest, engine_version=MODEL_VERSION,
            scenario_seed=impact.heldout_seed, stress_set_id=stress.id, stress_set_source=stress.source,
            scenarios_checked=len(scenarios), source=base_twin.provenance.get("balances", "fixture"),
            assumptions=impact.assumptions, request_scenario=request_scenario),
        latency_ms=int((time.perf_counter() - started) * 1000))

    if not persist:
        return result

    # ---- persist: fixes are approvable / executable by id; the run is the audit record
    for f in result.fixes:
        f.snapshot_id = base_twin.snapshot_id
        f.run_id = run_id
        store.save(f, actor="engine", action="fix.create", correlation_id=run_id)
    store.save(AnalysisRun(id=run_id, kind="simulation", created_at=utcnow(),
                           result=result.model_dump(mode="json", exclude={"impact"})),
               actor="engine", action="simulation.decide", correlation_id=bill.id)
    return result
