"""Bill impact analysis: "what does paying this bill on day d do to everything else?"

For each legal candidate pay day d:
  1. Build the twin with the bill paid on d (counterfactual; nothing is stored as fact).
  2. No-action replay -> min cash, first buffer breach, risk flags.
  3. Nominal plan: cheapest funding under the expected-timing scenario.
  4. Robust plan: cheapest funding that also survives the adverse scenario
     (all receipts at their (1 - risk_tolerance) delay quantile).
  5. Both plans are frozen and evaluated on HELD-OUT Monte Carlo seeds.
  6. The option uses the nominal plan if its held-out breach rate is within
     tolerance, else the robust plan; status is feasible / requires_review / infeasible.
The recommendation is the cheapest feasible option (ties -> lower risk, later date).
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from typing import Literal, Sequence

from pydantic import BaseModel, Field

from ..engine.models import Plan, PlanAction, Scenario, StressCheck, Violation, fmt_usd
from ..engine.optimizer import optimize_plan
from ..engine.scenarios import (
    HELDOUT_SEED_BASE,
    MonteCarloSummary,
    adverse_scenario,
    evaluate_monte_carlo,
    sample_scenarios,
)
from ..engine.simulator import simulate
from ..workflows.ap import get_bill
from ..workflows.forecast import build_twin_from_store, day_index
from ..workflows.models import APBill
from ..workflows.policy import PolicyResult, evaluate_bill
from ..workflows.state import WorkflowError
from ..workflows.store import Store

OptionStatus = Literal["feasible", "requires_review", "infeasible"]


class ImpactRequest(BaseModel):
    candidate_payment_days: list[int] | None = None   # default: earliest, earliest+1, due
    scenario_count: int = Field(default=200, ge=10, le=2000)
    risk_tolerance: float = Field(default=0.05, gt=0, lt=0.5)
    seed: int = 42


class PlanSummary(BaseModel):
    plan_id: str
    status: str
    actions: list[PlanAction]
    transfer_total_cents: int
    credit_draw_total_cents: int
    modeled_cost_cents: int
    heldout_breach_rate: float
    heldout_missed_obligation_rate: float
    heldout_worst_shortfall_cents: int
    violations: list[Violation]
    binding_constraints: list[str]
    stress_checks: list[StressCheck]


class ImpactOption(BaseModel):
    pay_day: int
    pay_date: date
    status: OptionStatus
    selected: Literal["nominal", "robust", "none"]
    min_available_cash_cents: int        # source account, no funding action
    first_buffer_breach_day: int | None  # any account, no funding action
    risk_flags: list[str]
    no_action_breach_rate: float
    nominal: PlanSummary
    robust: PlanSummary
    summary: str


class ImpactResult(BaseModel):
    run_id: str
    bill_id: str
    snapshot_id: str
    status: OptionStatus
    recommended_pay_day: int | None
    recommendation: str
    options: list[ImpactOption]
    policy: PolicyResult
    adverse_scenario: Scenario
    scenario_count: int
    planning_seed_note: str
    heldout_seed: int
    assumptions: list[str]


def _summarize(plan: Plan, twin, heldout: list[Scenario]) -> PlanSummary:
    mc = evaluate_monte_carlo(twin, plan.actions, heldout)
    return PlanSummary(
        plan_id=plan.id, status=plan.status, actions=plan.actions,
        transfer_total_cents=plan.transfer_total_cents,
        credit_draw_total_cents=plan.credit_draw_total_cents,
        modeled_cost_cents=plan.modeled_cost_cents, heldout_breach_rate=mc.breach_rate,
        heldout_missed_obligation_rate=mc.missed_obligation_rate,
        heldout_worst_shortfall_cents=mc.worst_shortfall_cents, violations=plan.violations,
        binding_constraints=plan.binding_constraints, stress_checks=plan.stress_checks)


def _risk_flags(result, twin) -> list[str]:
    flags: set[str] = set()
    obligations = {o.id: o for o in twin.obligations}
    for o in result.obligations:
        if o.buffer_breached:
            cat = (obligations[o.obligation_id].category or "").upper()
            flags.add(f"{cat}_BUFFER_BREACH" if cat in ("PAYROLL", "TAX") else "BUFFER_BREACH")
        if not o.covered and not o.deferrable:
            flags.add("MANDATORY_OBLIGATION_UNCOVERED")
    if result.metrics.breach:
        flags.add("BUFFER_BREACH")
    return sorted(flags)


def candidate_days(store: Store, bill: APBill, requested: list[int] | None) -> list[int]:
    cfg = store.get_config()
    lo = max(0, day_index(cfg, bill.earliest_pay_date))
    hi = day_index(cfg, bill.due_date)
    if hi < 0:
        raise WorkflowError("past_due", "bill is past due; schedule it for today", 400)
    days = requested if requested is not None else [lo, lo + 1, hi]
    legal = sorted({d for d in days if lo <= d <= hi and d < cfg.horizon_days})
    if not legal:
        raise WorkflowError("no_legal_dates", f"no candidate day within the legal window "
                            f"[{lo}, {hi}] and the {cfg.horizon_days}-day horizon", 400)
    return legal


def analyze_bill_impact(store: Store, bill_id: str, req: ImpactRequest, *,
                        bill: APBill | None = None,
                        stress: Sequence[Scenario] = ()) -> ImpactResult:
    """`bill` lets callers analyze an ephemeral (unstored) bill such as an ad-hoc payment.
    `stress` adds scenarios the robust plan must also survive (e.g. discovered shocks)."""
    ephemeral = bill is not None
    bill = bill or get_bill(store, bill_id)
    if bill.status in ("paid", "rejected"):
        raise WorkflowError("closed", f"bill is {bill.status}", 409)
    cfg = store.get_config()
    days = candidate_days(store, bill, req.candidate_payment_days)
    heldout_seed = HELDOUT_SEED_BASE + req.seed
    extra = [bill] if ephemeral else None

    options: list[ImpactOption] = []
    adverse = None
    snapshot_id = ""
    for d in days:
        twin = build_twin_from_store(store, bill_pay_days={bill.id: d}, extra_bills=extra)
        snapshot_id = snapshot_id or build_twin_from_store(store).snapshot_id
        adverse = adverse_scenario(twin, 1 - req.risk_tolerance)
        heldout = sample_scenarios(twin, req.scenario_count, seed=heldout_seed)

        nominal_run = simulate(twin)
        source_rows = [r for r in nominal_run.ledger if r.account_id == bill.source_account_id]

        nominal = _summarize(optimize_plan(twin), twin, heldout)
        # The no-action replay is the nominal plan when the LP needed no actions: reuse it.
        no_action = (MonteCarloSummary.from_summary(nominal) if not nominal.actions
                     else evaluate_monte_carlo(twin, [], heldout))
        # The robust plan is only needed when the nominal one is infeasible or too risky.
        if nominal.status == "feasible" and nominal.heldout_breach_rate <= req.risk_tolerance:
            robust = nominal
        else:
            robust = _summarize(optimize_plan(twin, stress=[adverse, *stress]), twin, heldout)

        if nominal.status == "feasible" and nominal.heldout_breach_rate <= req.risk_tolerance:
            selected, status = "nominal", "feasible"
        elif robust.status == "feasible":
            selected = "robust"
            status = "feasible" if robust.heldout_breach_rate <= req.risk_tolerance else "requires_review"
        elif nominal.status == "feasible":
            selected, status = "nominal", "requires_review"
        else:
            selected, status = "none", "infeasible"

        chosen = robust if selected == "robust" else nominal
        if status == "infeasible":
            short = sum(v.shortfall_cents for v in nominal.violations)
            summary = f"Pay day {d}: no feasible funding plan; {fmt_usd(short)} of buffer shortfall remains."
        else:
            summary = (f"Pay day {d}: fund with {fmt_usd(chosen.transfer_total_cents)} transfers and "
                       f"{fmt_usd(chosen.credit_draw_total_cents)} credit; modeled cost "
                       f"{fmt_usd(chosen.modeled_cost_cents)}; held-out breach rate "
                       f"{chosen.heldout_breach_rate:.0%} ({selected} plan).")
        first = nominal_run.metrics.first_breach
        options.append(ImpactOption(
            pay_day=d, pay_date=cfg.as_of + timedelta(days=d), status=status, selected=selected,
            min_available_cash_cents=min(r.closing_cents for r in source_rows),
            first_buffer_breach_day=first.day if first else None,
            risk_flags=_risk_flags(nominal_run, twin),
            no_action_breach_rate=no_action.breach_rate,
            nominal=nominal, robust=robust, summary=summary))

    def chosen_plan(o: ImpactOption) -> PlanSummary:
        return o.robust if o.selected == "robust" else o.nominal

    rank = {"feasible": 0, "requires_review": 1, "infeasible": 2}
    best = min(options, key=lambda o: (rank[o.status], chosen_plan(o).modeled_cost_cents,
                                       chosen_plan(o).heldout_breach_rate, -o.pay_day))
    if best.status == "infeasible":
        recommended, status = None, "infeasible"
        text = ("No candidate payment date can be funded within the modeled limits. "
                "See violations for the binding constraints.")
    else:
        recommended, status = best.pay_day, best.status
        text = f"Recommend paying on {best.pay_date.isoformat()} (day {best.pay_day}). {best.summary}"
        if best.status == "requires_review":
            text += " Residual risk exceeds the tolerance: requires review."

    run_payload = json.dumps({"bill": bill.model_dump(mode="json"), "req": req.model_dump(),
                              "snap": snapshot_id}, sort_keys=True)
    return ImpactResult(
        run_id="run_" + hashlib.sha256(run_payload.encode()).hexdigest()[:12],
        bill_id=bill.id, snapshot_id=snapshot_id, status=status,
        recommended_pay_day=recommended, recommendation=text, options=options,
        policy=evaluate_bill(cfg, store, bill), adverse_scenario=adverse,  # type: ignore[arg-type]
        scenario_count=req.scenario_count,
        planning_seed_note="Plans use the expected-timing and adverse scenarios only; "
                           "held-out Monte Carlo seeds are never used for planning.",
        heldout_seed=heldout_seed,
        assumptions=[
            f"Robust plans must survive '{adverse.id}': {adverse.description}." if adverse else "",
            "Breach rates are frequencies under synthetic delay distributions, not calibrated probabilities.",
            *cfg.assumptions,
        ])
