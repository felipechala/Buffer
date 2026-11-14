"""Planner (blue team): multi-day linear program solved with SciPy HiGHS.

Decision variables (all in cents):
  x[r,t]  transfer dispatched on allowed route r on day t   0 <= x <= route cap
  u[c,t]  credit draw requested on line c on day t           0 <= u, sum_t u <= available
  s[i,t]  buffer slack for account i at close of day t       s >= 0 (heavily penalized)

Closing balance is linear in the decisions:
  B[i,t] = C0[i,t] - sum_{r out of i, tau<=t} x[r,tau]
                   + sum_{r into i, tau+d_r<=t} x[r,tau]
                   + sum_{c at i, tau+d_c<=t} u[c,tau]
where C0 is the no-action closing balance from the simulator under the same scenario.
Constraint: B[i,t] + s[i,t] >= buffer_i.

Objective: fees + tiny time preference (act earlier, avoid pointless zero-fee moves)
+ SLACK_PENALTY * slack. Any positive slack means no feasible plan exists; the slack
values become a precise infeasibility report instead of a fabricated recommendation.

Every solution is independently re-verified by running it through the simulator.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import lil_matrix

from .schemas import (
    CreditDrawAction,
    Plan,
    PlanAction,
    Scenario,
    SolverInfo,
    TransferAction,
    TwinState,
    Violation,
    fmt_usd,
)
from .simulator import simulate

SLACK_PENALTY = 1_000.0   # cost per cent of buffer shortfall (fees are < 0.1 per cent)
TIME_EPS = 1e-7           # per cent per day; breaks ties toward earlier action

BASE_ASSUMPTIONS = [
    "Planned against a single scenario; feasibility holds only under its stated assumptions.",
    "Event order per day: receipts & settlements -> planned actions -> unexpected outflows -> obligations.",
    "Route caps apply per dispatch day; credit limits apply cumulatively over the horizon.",
    "Fees are one-time treasury costs and are not deducted from account balances.",
    "Expected receipts are forecasts, not posted cash.",
]


def _plan_id(snapshot_id: str, scenario: Scenario, actions: list[PlanAction]) -> str:
    payload = json.dumps(
        {"snapshot": snapshot_id, "scenario": scenario.model_dump(mode="json"),
         "actions": [a.model_dump(mode="json") for a in actions]},
        sort_keys=True,
    )
    return "plan_" + hashlib.sha256(payload.encode()).hexdigest()[:12]


def optimize_plan(twin: TwinState, scenario: Scenario | None = None,
                  horizon: int | None = None) -> Plan:
    scenario = scenario or Scenario()
    H = horizon or twin.horizon_days
    baseline = simulate(twin, scenario, [], H)
    closing0 = {(r.account_id, r.day): r.closing_cents for r in baseline.ledger}

    accounts = twin.accounts
    acct_index = {a.id: i for i, a in enumerate(accounts)}
    routes = [r for r in twin.routes if r.allowed and r.id not in scenario.disabled_routes]
    lines = [c for c in twin.credit_lines
             if c.enabled and c.id not in scenario.disabled_credit_lines]

    # ---- variables
    act_vars: list[tuple[str, object, int]] = (
        [("x", r, t) for r in routes for t in range(H)]
        + [("u", c, t) for c in lines for t in range(H)]
    )
    n_act = len(act_vars)
    n_slack = len(accounts) * H
    n = n_act + n_slack

    cost = np.zeros(n)
    bounds: list[tuple[float, float | None]] = []
    for k, (kind, obj, t) in enumerate(act_vars):
        cost[k] = obj.fee_rate + TIME_EPS * (1 + t)  # type: ignore[attr-defined]
        cap = obj.max_amount_cents if kind == "x" else obj.available_cents  # type: ignore[attr-defined]
        bounds.append((0.0, float(cap)))
    cost[n_act:] = SLACK_PENALTY
    bounds += [(0.0, None)] * n_slack

    # ---- constraints (A_ub @ v <= b_ub)
    def row(i: int, t: int) -> int:
        return i * H + t

    n_rows = len(accounts) * H + len(lines)
    A = lil_matrix((n_rows, n))
    b = np.zeros(n_rows)
    for i, a in enumerate(accounts):
        for t in range(H):
            b[row(i, t)] = closing0[(a.id, t)] - a.minimum_buffer_cents
            A[row(i, t), n_act + row(i, t)] = -1.0  # slack

    line_row = {c.id: len(accounts) * H + j for j, c in enumerate(lines)}
    for c in lines:
        b[line_row[c.id]] = c.available_cents

    for k, (kind, obj, tau) in enumerate(act_vars):
        if kind == "x":
            src, dst = acct_index[obj.source_id], acct_index[obj.destination_id]  # type: ignore[attr-defined]
            for t in range(tau, H):
                A[row(src, t), k] += 1.0
            for t in range(tau + obj.settlement_days, H):  # type: ignore[attr-defined]
                A[row(dst, t), k] -= 1.0
        else:
            dst = acct_index[obj.account_id]  # type: ignore[attr-defined]
            for t in range(tau + obj.draw_delay_days, H):  # type: ignore[attr-defined]
                A[row(dst, t), k] -= 1.0
            A[line_row[obj.id], k] = 1.0  # type: ignore[attr-defined]

    started = time.perf_counter()
    res = linprog(cost, A_ub=A.tocsr(), b_ub=b, bounds=bounds, method="highs")
    runtime_ms = (time.perf_counter() - started) * 1000
    solver = SolverInfo(method="scipy.linprog/highs", status=int(res.status),
                        message=str(res.message), runtime_ms=round(runtime_ms, 2),
                        n_variables=n, n_constraints=n_rows,
                        objective=float(res.fun) if res.success else None)

    assumptions = BASE_ASSUMPTIONS + [f"Scenario: {scenario.id}"] + list(twin.assumptions)
    if not res.success:
        return Plan(id=_plan_id(twin.snapshot_id, scenario, []), snapshot_id=twin.snapshot_id,
                    scenario_id=scenario.id, generated_at=datetime.now(timezone.utc),
                    status="solver_error", actions=[], transfer_total_cents=0,
                    credit_draw_total_cents=0, modeled_cost_cents=0, violations=[],
                    binding_constraints=[], assumptions=assumptions, solver=solver,
                    verification=baseline.metrics)

    values = res.x
    # ---- extract actions (round to cents)
    actions: list[PlanAction] = []
    for k, (kind, obj, t) in enumerate(act_vars):
        amount = int(round(values[k]))
        if amount <= 0:
            continue
        if kind == "x":
            actions.append(TransferAction(route_id=obj.id, day=t, amount_cents=amount))  # type: ignore[attr-defined]
        else:
            actions.append(CreditDrawAction(credit_line_id=obj.id, day=t, amount_cents=amount))  # type: ignore[attr-defined]
    actions.sort(key=lambda a: (a.day, a.kind, getattr(a, "route_id", "") or getattr(a, "credit_line_id", "")))

    # ---- independent verification through the simulator
    check = simulate(twin, scenario, actions, H)

    slack = values[n_act:]
    violations: list[Violation] = []
    for i, a in enumerate(accounts):
        for t in range(H):
            s = slack[row(i, t)]
            if s > 0.5:
                violations.append(Violation(
                    account_id=a.id, day=t, shortfall_cents=int(round(s)),
                    explanations=_explain_shortfall(twin, scenario, a.id, t, actions)))

    if violations:
        status = "infeasible"
    elif check.metrics.breach or check.rejected_actions:
        status = "verification_failed"
    else:
        status = "feasible"

    return Plan(
        id=_plan_id(twin.snapshot_id, scenario, actions),
        snapshot_id=twin.snapshot_id,
        scenario_id=scenario.id,
        generated_at=datetime.now(timezone.utc),
        status=status,
        actions=actions,
        transfer_total_cents=sum(a.amount_cents for a in actions if a.kind == "transfer"),
        credit_draw_total_cents=sum(a.amount_cents for a in actions if a.kind == "credit_draw"),
        modeled_cost_cents=check.metrics.fees_cents,
        violations=violations,
        binding_constraints=_binding_constraints(twin, actions, check.ledger),
        assumptions=assumptions,
        solver=solver,
        verification=check.metrics,
    )


def _explain_shortfall(twin: TwinState, scenario: Scenario, account_id: str, day: int,
                       actions: list[PlanAction]) -> list[str]:
    """Name the funding limits that stop the account from reaching its buffer on `day`."""
    notes: list[str] = []
    for r in twin.routes:
        if r.destination_id != account_id:
            continue
        if not r.allowed:
            notes.append(f"Route {r.id} is not permitted ({r.note or 'policy restriction'}).")
        elif r.id in scenario.disabled_routes:
            notes.append(f"Route {r.id} is unavailable in this scenario.")
        elif r.settlement_days > day:
            notes.append(f"Route {r.id} settles in {r.settlement_days} day(s); the earliest "
                         f"arrival is day {r.settlement_days}, after the day-{day} deadline.")
        else:
            sent = sum(a.amount_cents for a in actions
                       if a.kind == "transfer" and a.route_id == r.id
                       and a.day + r.settlement_days <= day)
            capacity = r.max_amount_cents * (day - r.settlement_days + 1)
            if sent >= capacity - 1:
                notes.append(f"Route {r.id} is at its cap ({fmt_usd(r.max_amount_cents)}/day).")
            else:
                notes.append(f"Route {r.id} is limited by cash above the buffer in "
                             f"{r.source_id} ({fmt_usd(sent)} usable by day {day}).")
    for c in twin.credit_lines:
        if c.account_id != account_id:
            continue
        if not c.enabled or c.id in scenario.disabled_credit_lines:
            notes.append(f"Credit line {c.id} is unavailable.")
        elif c.draw_delay_days > day:
            notes.append(f"Credit line {c.id} funds after {c.draw_delay_days} day(s), too late for day {day}.")
        else:
            drawn = sum(a.amount_cents for a in actions
                        if a.kind == "credit_draw" and a.credit_line_id == c.id)
            if drawn >= c.available_cents - 1:
                notes.append(f"Credit line {c.id} is fully drawn ({fmt_usd(c.available_cents)} available).")
    if not notes:
        notes.append(f"No transfer route or credit line can fund {account_id}.")
    return notes


def _binding_constraints(twin: TwinState, actions: list[PlanAction], ledger) -> list[str]:
    out: list[str] = []
    for a in actions:
        if a.kind == "transfer":
            r = twin.route(a.route_id)
            if r and a.amount_cents >= r.max_amount_cents - 1:
                out.append(f"Route {r.id} at its daily cap {fmt_usd(r.max_amount_cents)} on day {a.day}.")
    for c in twin.credit_lines:
        drawn = sum(a.amount_cents for a in actions
                    if a.kind == "credit_draw" and a.credit_line_id == c.id)
        if drawn and drawn >= c.available_cents - 1:
            out.append(f"Credit line {c.id} fully drawn ({fmt_usd(c.available_cents)}).")
    tight_days: dict[str, list[int]] = {}
    for rowx in ledger:
        if rowx.closing_cents == rowx.buffer_cents and rowx.buffer_cents > 0:
            tight_days.setdefault(rowx.account_id, []).append(rowx.day)
    for account_id, days in tight_days.items():
        buf = twin.account(account_id).minimum_buffer_cents
        out.append(f"{account_id} held exactly at its {fmt_usd(buf)} buffer from day {days[0]}.")
    return out
