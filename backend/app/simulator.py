"""Deterministic multi-account cash simulator.

Intra-day event order (applied to every day t, for every account):
  1. Inflows: expected receipts landing on t (after scenario delays/haircuts),
     transfer arrivals settling on t, credit draws funding on t.
  2. Planned actions dispatched on t: transfers debit the source immediately;
     the destination is credited on t + settlement_days (same day if 0).
     Credit draws are funded on t + draw_delay_days.
  3. Unexpected outflows (scenario shocks).
  4. Scheduled obligations, ordered by (priority, id).
  5. Close: record balances; buffer shortfall = max(0, buffer - closing).

Because outflows come last, the closing balance is the lowest point of the day
after planned actions, so buffer checks at close are the binding ones.

Fees are modeled as a treasury cost (reported in metrics), not deducted from
account balances. Internal transfers never create cash: money in flight is
tracked in `in_transit_by_day` and counted in firm-wide total cash.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Sequence

from .schemas import (
    AccountDay,
    BreachPoint,
    CreditDrawAction,
    ObligationOutcome,
    PlanAction,
    RejectedAction,
    Scenario,
    SimMetrics,
    SimulationResult,
    TransferAction,
    TwinState,
    fee_cents,
)


def validate_actions(
    twin: TwinState, scenario: Scenario, actions: Iterable[PlanAction], horizon: int
) -> tuple[list[PlanAction], list[RejectedAction]]:
    """Split actions into accepted / rejected using explicit constraint checks."""
    accepted: list[PlanAction] = []
    rejected: list[RejectedAction] = []
    route_day_used: dict[tuple[str, int], int] = defaultdict(int)
    credit_used: dict[str, int] = defaultdict(int)

    for act in actions:
        reason = None
        if act.day >= horizon:
            reason = f"day {act.day} is outside the {horizon}-day horizon"
        elif isinstance(act, TransferAction):
            route = twin.route(act.route_id)
            if route is None:
                reason = f"unknown route {act.route_id}"
            elif not route.allowed:
                reason = f"route {route.id} is not an allowed route"
            elif route.id in scenario.disabled_routes:
                reason = f"route {route.id} is unavailable in scenario {scenario.id}"
            elif route_day_used[(route.id, act.day)] + act.amount_cents > route.max_amount_cents:
                reason = (f"route {route.id} daily cap {route.max_amount_cents} cents "
                          f"exceeded on day {act.day}")
            else:
                route_day_used[(route.id, act.day)] += act.amount_cents
        elif isinstance(act, CreditDrawAction):
            line = twin.credit_line(act.credit_line_id)
            if line is None:
                reason = f"unknown credit line {act.credit_line_id}"
            elif not line.enabled:
                reason = f"credit line {line.id} is disabled"
            elif line.id in scenario.disabled_credit_lines:
                reason = f"credit line {line.id} is unavailable in scenario {scenario.id}"
            elif credit_used[line.id] + act.amount_cents > line.available_cents:
                reason = f"credit line {line.id} limit ({line.available_cents} cents available) exceeded"
            else:
                credit_used[line.id] += act.amount_cents
        else:  # pragma: no cover - schema prevents this
            reason = "unknown action type"

        if reason is None:
            accepted.append(act)
        else:
            rejected.append(RejectedAction(action=act, reason=reason))
    return accepted, rejected


def simulate(
    twin: TwinState,
    scenario: Scenario | None = None,
    actions: Sequence[PlanAction] = (),
    horizon: int | None = None,
) -> SimulationResult:
    """Pure function: same inputs always give the same result; inputs are not mutated."""
    scenario = scenario or Scenario()
    H = horizon or twin.horizon_days
    acct_ids = [a.id for a in twin.accounts]
    buffers = {a.id: a.minimum_buffer_cents for a in twin.accounts}
    bal = {a.id: a.observed_balance_cents for a in twin.accounts}

    # Expected receipts under the scenario.
    receipt_inflows: dict[tuple[int, str], int] = defaultdict(int)
    beyond_horizon: list[str] = []
    for r in twin.receipts:
        day = r.expected_day + scenario.receipt_delays.get(r.id, 0)
        lost = round(r.amount_cents * scenario.receipt_haircuts.get(r.id, 0.0))
        amount = r.amount_cents - lost
        if amount <= 0:
            continue
        if day >= H:
            beyond_horizon.append(r.id)
            continue
        receipt_inflows[(day, r.account_id)] += amount

    accepted, rejected = validate_actions(twin, scenario, actions, H)
    actions_by_day: dict[int, list[PlanAction]] = defaultdict(list)
    for act in accepted:
        actions_by_day[act.day].append(act)

    obligations_by_day = defaultdict(list)
    for o in sorted(twin.obligations, key=lambda o: (o.due_day, o.priority, o.id)):
        if o.due_day < H:
            obligations_by_day[o.due_day].append(o)

    outflows_by_day = defaultdict(list)
    for s in scenario.unexpected_outflows:
        if s.day < H:
            outflows_by_day[s.day].append(s)

    pending_transfers: dict[tuple[int, str], int] = defaultdict(int)
    pending_financing: dict[tuple[int, str], int] = defaultdict(int)
    in_transit = 0
    fees = 0

    ledger: list[AccountDay] = []
    outcomes: list[ObligationOutcome] = []
    total_cash_by_day: list[int] = []
    in_transit_by_day: list[int] = []

    for t in range(H):
        rows = {a: AccountDay(account_id=a, day=t, opening_cents=bal[a],
                              buffer_cents=buffers[a]) for a in acct_ids}

        # 1. inflows
        for a in acct_ids:
            rec = receipt_inflows.get((t, a), 0)
            arr = pending_transfers.pop((t, a), 0)
            fin = pending_financing.pop((t, a), 0)
            bal[a] += rec + arr + fin
            in_transit -= arr
            rows[a].receipts_cents += rec
            rows[a].transfer_arrivals_cents += arr
            rows[a].financing_cents += fin

        # 2. planned actions
        for act in actions_by_day.get(t, []):
            if isinstance(act, TransferAction):
                route = twin.route(act.route_id)
                assert route is not None
                fees += fee_cents(act.amount_cents, route.fee_rate)
                bal[route.source_id] -= act.amount_cents
                rows[route.source_id].transfer_out_cents += act.amount_cents
                if route.settlement_days == 0:
                    bal[route.destination_id] += act.amount_cents
                    rows[route.destination_id].transfer_arrivals_cents += act.amount_cents
                else:
                    pending_transfers[(t + route.settlement_days, route.destination_id)] += act.amount_cents
                    in_transit += act.amount_cents
            else:
                line = twin.credit_line(act.credit_line_id)
                assert line is not None
                fees += fee_cents(act.amount_cents, line.fee_rate)
                if line.draw_delay_days == 0:
                    bal[line.account_id] += act.amount_cents
                    rows[line.account_id].financing_cents += act.amount_cents
                else:
                    pending_financing[(t + line.draw_delay_days, line.account_id)] += act.amount_cents

        # 3. unexpected outflows
        for s in outflows_by_day.get(t, []):
            bal[s.account_id] -= s.amount_cents
            rows[s.account_id].unexpected_out_cents += s.amount_cents

        # 4. obligations
        for o in obligations_by_day.get(t, []):
            before = bal[o.account_id]
            bal[o.account_id] -= o.amount_cents
            rows[o.account_id].payments_cents += o.amount_cents
            outcomes.append(ObligationOutcome(
                obligation_id=o.id, account_id=o.account_id, due_day=t,
                amount_cents=o.amount_cents, deferrable=o.deferrable,
                balance_before_cents=before, balance_after_cents=bal[o.account_id],
                covered=before >= o.amount_cents,
                buffer_breached=bal[o.account_id] < buffers[o.account_id],
            ))

        # 5. close
        for a in acct_ids:
            rows[a].closing_cents = bal[a]
            rows[a].shortfall_cents = max(0, buffers[a] - bal[a])
            ledger.append(rows[a])
        in_transit_by_day.append(in_transit)
        total_cash_by_day.append(sum(bal.values()) + in_transit)

    return SimulationResult(
        scenario_id=scenario.id,
        snapshot_id=twin.snapshot_id,
        horizon_days=H,
        ledger=ledger,
        obligations=outcomes,
        rejected_actions=rejected,
        receipts_beyond_horizon=beyond_horizon,
        total_cash_by_day=total_cash_by_day,
        in_transit_by_day=in_transit_by_day,
        metrics=_metrics(ledger, outcomes, fees),
    )


def _metrics(ledger: list[AccountDay], outcomes: list[ObligationOutcome], fees: int) -> SimMetrics:
    breaches = [r for r in ledger if r.shortfall_cents > 0]
    first = min(breaches, key=lambda r: (r.day, r.account_id), default=None)
    uncovered = [o for o in outcomes if not o.covered]
    return SimMetrics(
        breach=bool(breaches),
        first_breach=BreachPoint(account_id=first.account_id, day=first.day,
                                 shortfall_cents=first.shortfall_cents) if first else None,
        worst_shortfall_cents=max((r.shortfall_cents for r in ledger), default=0),
        shortfall_cent_days=sum(r.shortfall_cents for r in ledger),
        uncovered_obligations=[o.obligation_id for o in uncovered],
        uncovered_mandatory_cents=sum(o.amount_cents for o in uncovered if not o.deferrable),
        fees_cents=fees,
        min_headroom_cents=min((r.closing_cents - r.buffer_cents for r in ledger), default=0),
    )
