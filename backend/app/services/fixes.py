"""Fix generation and verification for a proposed payment.

A fix is one of:
  * reschedule          pay on a later legal day; no funding actions needed
  * funding             pay on the requested day; transfers / credit draws make it safe
  * reschedule+funding  later day that still needs a (smaller) plan

Funding plans come from the LP (nominal, robust, and "forbid one action type" variants).
Every fix is replayed through the simulator in exact cents under the baseline and under
every scenario in the stress set; only clean replays are `verified`.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Literal, Sequence, Union

from pydantic import BaseModel, Field

from ..engine.models import (
    CreditDrawAction,
    Plan,
    PlanAction,
    Scenario,
    StressCheck,
    TransferAction,
    TwinState,
    fmt_usd,
)
from ..engine.optimizer import optimize_plan
from ..engine.simulator import simulate
from .impact import ImpactOption, PlanSummary


class ScheduleBillFix(BaseModel):
    kind: Literal["schedule_bill"] = "schedule_bill"
    bill_id: str
    amount_cents: int
    pay_day: int
    pay_date: date


class TransferFix(BaseModel):
    kind: Literal["transfer"] = "transfer"
    route_id: str
    from_account: str
    to_account: str
    amount_cents: int
    dispatch_day: int
    dispatch_date: date
    arrival_day: int
    fee_cents: int


class CreditDrawFix(BaseModel):
    kind: Literal["credit_draw"] = "credit_draw"
    line_id: str
    account: str
    amount_cents: int
    day: int
    date: date
    funds_day: int
    fee_cents: int


FixAction = Union[ScheduleBillFix, TransferFix, CreditDrawFix]


class HeldOut(BaseModel):
    breach_rate: float
    paths: int


class Fix(BaseModel):
    id: str                                  # approvable / executable id (stored by decision.decide)
    bill_id: str = ""
    snapshot_id: str = ""                    # base snapshot the fix was computed against
    run_id: str = ""
    kind: Literal["reschedule", "funding", "reschedule+funding"]
    summary: str
    pay_day: int
    pay_date: date
    delay_days: int
    cost_cents: int
    actions: list[FixAction] = Field(default_factory=list)
    verified: bool
    held_out: HeldOut
    stress_checks: list[StressCheck]
    plan_actions: list[PlanAction] = Field(default_factory=list)   # engine actions for replay/execution


def _fee(amount: int, rate: float) -> int:
    return int(round(amount * rate))


def describe_actions(twin: TwinState, actions: Sequence[PlanAction]) -> list[FixAction]:
    out: list[FixAction] = []
    for a in actions:
        if isinstance(a, TransferAction):
            r = twin.route(a.route_id)
            assert r is not None
            out.append(TransferFix(route_id=r.id, from_account=r.source_id, to_account=r.destination_id,
                                   amount_cents=a.amount_cents, dispatch_day=a.day,
                                   dispatch_date=twin.as_of + timedelta(days=a.day),
                                   arrival_day=a.day + r.settlement_days,
                                   fee_cents=_fee(a.amount_cents, r.fee_rate)))
        elif isinstance(a, CreditDrawAction):
            c = twin.credit_line(a.credit_line_id)
            assert c is not None
            out.append(CreditDrawFix(line_id=c.id, account=c.account_id, amount_cents=a.amount_cents,
                                     day=a.day, date=twin.as_of + timedelta(days=a.day),
                                     funds_day=a.day + c.draw_delay_days,
                                     fee_cents=_fee(a.amount_cents, c.fee_rate)))
    return out


def replay_under(twin: TwinState, actions: Sequence[PlanAction],
                 scenarios: Sequence[Scenario]) -> list[StressCheck]:
    checks = []
    for s in [Scenario(), *scenarios]:
        r = simulate(twin, s, actions)
        checks.append(StressCheck(scenario_id=s.id, breach=r.metrics.breach,
                                  worst_shortfall_cents=r.metrics.worst_shortfall_cents,
                                  min_headroom_cents=r.metrics.min_headroom_cents,
                                  rejected_actions=len(r.rejected_actions)))
    return checks


def is_clean(checks: Sequence[StressCheck]) -> bool:
    return all(not c.breach and c.rejected_actions == 0 for c in checks)


def summarize(payee: str, requested_day: int, fix_actions: Sequence[FixAction], twin: TwinState,
              pay_day: int) -> str:
    requested_date = (twin.as_of + timedelta(days=requested_day)).strftime("%b %d")
    pay_date = (twin.as_of + timedelta(days=pay_day)).strftime("%b %d")
    parts: list[str] = []
    if pay_day != requested_day:
        parts.append(f"Pay {payee} on {pay_date} instead of {requested_date}")
    else:
        parts.append(f"Pay {payee} on {requested_date}")
    for a in fix_actions:
        if isinstance(a, TransferFix):
            parts.append(f"transfer {fmt_usd(a.amount_cents)} {a.from_account} → {a.to_account} "
                         f"on {a.dispatch_date.strftime('%b %d')}")
        elif isinstance(a, CreditDrawFix):
            parts.append(f"draw {fmt_usd(a.amount_cents)} on {a.line_id} on {a.date.strftime('%b %d')}")
    return "; ".join(parts)


def funding_variants(twin: TwinState, stress: Sequence[Scenario]) -> list[Plan]:
    """Robust plan plus 'forbid one action type' alternatives. Infeasible variants are dropped."""
    variants: list[Plan] = []
    base = optimize_plan(twin, stress=stress)
    variants.append(base)
    all_lines = [c.id for c in twin.credit_lines]
    all_routes = [r.id for r in twin.routes]
    if all_lines and any(a.kind == "credit_draw" for a in base.actions):
        variants.append(optimize_plan(twin, Scenario(id="no-credit", disabled_credit_lines=all_lines),
                                      stress=stress))
    if all_routes and any(a.kind == "transfer" for a in base.actions):
        variants.append(optimize_plan(twin, Scenario(id="no-transfers", disabled_routes=all_routes),
                                      stress=stress))
    seen: set[tuple] = set()
    out: list[Plan] = []
    for p in variants:
        if p.status != "feasible" or not p.actions:
            continue
        key = tuple(sorted((a.kind, getattr(a, "route_id", getattr(a, "credit_line_id", "")),
                            a.day, a.amount_cents) for a in p.actions))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def build_fixes(*, payee: str, bill_id: str, amount_cents: int, requested_day: int,
                options: Sequence[ImpactOption], twins: dict[int, TwinState],
                stress: Sequence[Scenario], risk_tolerance: float, heldout_paths: int,
                max_fixes: int = 3) -> list[Fix]:
    """`twins[d]` is the counterfactual twin with the bill paid on day d."""
    fixes: list[Fix] = []
    seen: set[tuple] = set()

    def add(plan_id: str, kind: str, pay_day: int, actions: Sequence[PlanAction],
            cost: int, summary: PlanSummary | None) -> None:
        twin = twins[pay_day]
        key = (pay_day, tuple((a.kind, a.day, a.amount_cents) for a in actions))
        if key in seen:
            return
        seen.add(key)
        checks = replay_under(twin, actions, stress)
        fix_actions = describe_actions(twin, actions)
        if pay_day != requested_day:
            fix_actions.insert(0, ScheduleBillFix(bill_id=bill_id, amount_cents=amount_cents,
                                                  pay_day=pay_day,
                                                  pay_date=twin.as_of + timedelta(days=pay_day)))
        held = HeldOut(breach_rate=summary.heldout_breach_rate if summary else 0.0,
                       paths=heldout_paths)
        fixes.append(Fix(
            id=plan_id, bill_id=bill_id, kind=kind, pay_day=pay_day, pay_date=twin.as_of + timedelta(days=pay_day),
            delay_days=pay_day - requested_day, cost_cents=cost,
            summary=summarize(payee, requested_day, fix_actions, twin, pay_day),
            actions=fix_actions, verified=is_clean(checks), held_out=held, stress_checks=checks,
            plan_actions=list(actions)))

    # 1. Reschedule with no funding: earliest later day whose no-action replay is clean.
    for o in sorted(options, key=lambda o: o.pay_day):
        if o.pay_day <= requested_day or o.status == "infeasible":
            continue
        if o.nominal.actions or o.nominal.heldout_breach_rate > risk_tolerance:
            continue
        if is_clean(replay_under(twins[o.pay_day], [], stress)):
            add(f"plan_resched_{bill_id}_d{o.pay_day}", "reschedule", o.pay_day, [], 0, o.nominal)
            break

    # 2. Funding on the requested day: robust LP and forbid-one variants.
    requested = next((o for o in options if o.pay_day == requested_day), None)
    if requested is not None and requested.status != "infeasible":
        for plan in funding_variants(twins[requested_day], stress):
            add(plan.id, "funding", requested_day, plan.actions, plan.modeled_cost_cents, None)

    # 3. Reschedule + funding, only if we still have room.
    if len(fixes) < max_fixes:
        for o in sorted(options, key=lambda o: o.pay_day):
            if o.pay_day <= requested_day or o.status == "infeasible" or o.selected == "none":
                continue
            chosen = o.robust if o.selected == "robust" else o.nominal
            if not chosen.actions:
                continue
            add(chosen.plan_id, "reschedule+funding", o.pay_day, chosen.actions,
                chosen.modeled_cost_cents, chosen)
            break

    verified = [f for f in fixes if f.verified]
    verified.sort(key=lambda f: (f.cost_cents, f.delay_days, f.held_out.breach_rate))
    return verified[:max_fixes]
