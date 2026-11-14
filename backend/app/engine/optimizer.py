"""Optimizer: multi-day, optionally scenario-robust linear program (SciPy HiGHS).

Decision variables (all in cents), shared across every scenario:
  x[r,t]  transfer dispatched on allowed route r on day t   0 <= x <= route cap
  u[c,t]  credit draw requested on line c on day t           0 <= u, sum_t u <= available
Per scenario k:
  s[k,i,t] buffer slack for account i at close of day t      s >= 0 (heavily penalized)

Closing balance is linear in the decisions:
  B[k,i,t] = C0[k,i,t] - sum_{r out of i, tau<=t} x[r,tau]
                       + sum_{r into i, tau+d_r<=t} x[r,tau]
                       + sum_{c at i, tau+d_c<=t} u[c,tau]
where C0[k] is the no-action closing balance from the simulator under scenario k.
Constraint: B[k,i,t] + s[k,i,t] >= buffer_i for every scenario, account and day.

Passing stress scenarios makes the plan conservative: one action schedule must keep
every buffer in every scenario. Any positive slack means no such plan exists; the slack
values become a precise infeasibility report instead of a fabricated recommendation.

Objective: fees + tiny time preference (act earlier, avoid pointless zero-fee moves)
+ SLACK_PENALTY * slack. Every solution is rounded to cents and replayed through the
simulator under each scenario before it can be called feasible.
"""
from __future__ import annotations


import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Sequence

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import coo_matrix

from .models import (
    CreditDrawAction,
    Plan,
    PlanAction,
    Scenario,
    SolverInfo,
    StressCheck,
    TransferAction,
    TwinState,
    Violation,
    fmt_usd,
)
from .simulator import simulate
from .solver_thread import _run_on_solver_thread

SLACK_PENALTY = 1_000.0   # cost per cent of buffer shortfall (fees are < 0.1 per cent)
TIME_EPS = 1e-7           # per cent per day; breaks ties toward earlier action
MODEL_VERSION = "optimizer-lp-0.2"

BASE_ASSUMPTIONS = [
    "Feasibility holds only under the listed scenarios and their stated assumptions.",
    "Event order per day: receipts & settlements -> planned actions -> unexpected outflows -> obligations.",
    "Route caps apply per dispatch day; credit limits apply cumulatively over the horizon.",
    "Fees are one-time treasury costs and are not deducted from account balances.",
    "Expected receipts are forecasts, not posted cash.",
]


def _plan_id(snapshot_id: str, scenarios: list[Scenario], actions: list[PlanAction]) -> str:
    payload = json.dumps(
        {"snapshot": snapshot_id, "model": MODEL_VERSION,
         "scenarios": [s.model_dump(mode="json") for s in scenarios],
         "actions": [a.model_dump(mode="json") for a in actions]},
        sort_keys=True,
    )
    return "plan_" + hashlib.sha256(payload.encode()).hexdigest()[:12]


class _SparseBuilder:
    """Dict-backed (row, col) -> value builder; ~10x faster than lil_matrix item assignment."""

    def __init__(self, shape: tuple[int, int]):
        self.shape = shape
        self._d: dict[tuple[int, int], float] = {}

    def __getitem__(self, key: tuple[int, int]) -> float:
        return self._d.get(key, 0.0)

    def __setitem__(self, key: tuple[int, int], value: float) -> None:
        self._d[key] = value

    def tocsr(self):
        if not self._d:
            return coo_matrix(self.shape).tocsr()
        rows, cols = zip(*self._d)
        return coo_matrix((list(self._d.values()), (rows, cols)), shape=self.shape).tocsr()


_PLAN_CACHE: dict[tuple, Plan] = {}


def optimize_plan(twin: TwinState, scenario: Scenario | None = None,
                  horizon: int | None = None, *,
                  stress: Sequence[Scenario] = ()) -> Plan:
    """Deterministic in its inputs, so results are memoized by twin content (callers get a private copy)."""
    key = (twin.snapshot_id, scenario.model_dump_json() if scenario else "", horizon,
           tuple(s.model_dump_json() for s in stress))
    hit = _PLAN_CACHE.get(key)
    if hit is None:
        if len(_PLAN_CACHE) > 256:
            _PLAN_CACHE.clear()
        hit = _PLAN_CACHE[key] = _optimize_plan(twin, scenario, horizon, stress=stress)
    return hit.model_copy(deep=True)


def _optimize_plan(twin: TwinState, scenario: Scenario | None = None,
                   horizon: int | None = None, *,
                   stress: Sequence[Scenario] = ()) -> Plan:
    primary = scenario or Scenario()
    scenarios = [primary] + [s for s in stress if s.id != primary.id]
    H = horizon or twin.horizon_days
    K = len(scenarios)
    disabled_routes = {r for s in scenarios for r in s.disabled_routes}
    disabled_lines = {c for s in scenarios for c in s.disabled_credit_lines}

    baselines = [simulate(twin, s, [], H) for s in scenarios]
    closing0 = [{(r.account_id, r.day): r.closing_cents for r in b.ledger} for b in baselines]

    accounts = twin.accounts
    n_acct = len(accounts)
    acct_index = {a.id: i for i, a in enumerate(accounts)}
    routes = [r for r in twin.routes if r.allowed and r.id not in disabled_routes]
    lines = [c for c in twin.credit_lines if c.enabled and c.id not in disabled_lines]

    # ---- variables
    act_vars: list[tuple[str, object, int]] = (
        [("x", r, t) for r in routes for t in range(H)]
        + [("u", c, t) for c in lines for t in range(H)]
    )
    n_act = len(act_vars)
    n_slack = K * n_acct * H
    n = n_act + n_slack

    def row(k: int, i: int, t: int) -> int:
        return (k * n_acct + i) * H + t

    cost = np.zeros(n)
    bounds: list[tuple[float, float | None]] = []
    for j, (kind, obj, t) in enumerate(act_vars):
        cost[j] = obj.fee_rate + TIME_EPS * (1 + t)  # type: ignore[attr-defined]
        cap = obj.max_amount_cents if kind == "x" else obj.available_cents  # type: ignore[attr-defined]
        bounds.append((0.0, float(cap)))
    cost[n_act:] = SLACK_PENALTY / K
    bounds += [(0.0, None)] * n_slack

    # ---- constraints (A_ub @ v <= b_ub)
    n_rows = n_slack + len(lines)
    A = _SparseBuilder((n_rows, n))
    b = np.zeros(n_rows)
    for k in range(K):
        for i, a in enumerate(accounts):
            for t in range(H):
                b[row(k, i, t)] = closing0[k][(a.id, t)] - a.minimum_buffer_cents
                A[row(k, i, t), n_act + row(k, i, t)] = -1.0

    line_row = {c.id: n_slack + j for j, c in enumerate(lines)}
    for c in lines:
        b[line_row[c.id]] = c.available_cents

    for j, (kind, obj, tau) in enumerate(act_vars):
        for k in range(K):
            if kind == "x":
                src, dst = acct_index[obj.source_id], acct_index[obj.destination_id]  # type: ignore[attr-defined]
                for t in range(tau, H):
                    A[row(k, src, t), j] += 1.0
                for t in range(tau + obj.settlement_days, H):  # type: ignore[attr-defined]
                    A[row(k, dst, t), j] -= 1.0
            else:
                dst = acct_index[obj.account_id]  # type: ignore[attr-defined]
                for t in range(tau + obj.draw_delay_days, H):  # type: ignore[attr-defined]
                    A[row(k, dst, t), j] -= 1.0
        if kind == "u":
            A[line_row[obj.id], j] = 1.0  # type: ignore[attr-defined]

    started = time.perf_counter()
    # HiGHS (SciPy 1.17) corrupts process state when run in a thread that later exits (Windows access
    # violation / Thread.start hang); run it on one persistent daemon thread, which also serializes solves.
    res = _run_on_solver_thread(linprog, cost, A_ub=A.tocsr(), b_ub=b, bounds=bounds, method="highs")
    runtime_ms = (time.perf_counter() - started) * 1000
    solver = SolverInfo(method="scipy.linprog/highs", model_version=MODEL_VERSION,
                        status=int(res.status), message=str(res.message),
                        runtime_ms=round(runtime_ms, 2), n_variables=n,
                        n_constraints=n_rows,
                        objective=float(res.fun) if res.success else None)

    assumptions = (BASE_ASSUMPTIONS
                   + [f"Scenario evaluated: {s.id}" + (f" ({s.description})" if s.description else "")
                      for s in scenarios]
                   + list(twin.assumptions))
    common = dict(snapshot_id=twin.snapshot_id, scenario_id=primary.id,
                  scenario_ids=[s.id for s in scenarios],
                  generated_at=datetime.now(timezone.utc), assumptions=assumptions, solver=solver)

    if not res.success:
        return Plan(id=_plan_id(twin.snapshot_id, scenarios, []), status="solver_error",
                    actionable=False, actions=[], transfer_total_cents=0,
                    credit_draw_total_cents=0, modeled_cost_cents=0, violations=[],
                    binding_constraints=[], verification=baselines[0].metrics,
                    stress_checks=[], **common)

    values = res.x
    # ---- extract actions at a controlled rounding boundary (integer cents)
    actions: list[PlanAction] = []
    for j, (kind, obj, t) in enumerate(act_vars):
        amount = int(round(values[j]))
        if amount <= 0:
            continue
        if kind == "x":
            actions.append(TransferAction(route_id=obj.id, day=t, amount_cents=amount))  # type: ignore[attr-defined]
        else:
            actions.append(CreditDrawAction(credit_line_id=obj.id, day=t, amount_cents=amount))  # type: ignore[attr-defined]
    actions.sort(key=lambda a: (a.day, a.kind, getattr(a, "route_id", "") or getattr(a, "credit_line_id", "")))

    # ---- violations reported by the LP (positive slack)
    slack = values[n_act:]
    violations: list[Violation] = []
    for k, sc in enumerate(scenarios):
        for i, a in enumerate(accounts):
            for t in range(H):
                s = slack[row(k, i, t)]
                if s > 0.5:
                    violations.append(Violation(
                        scenario_id=sc.id, account_id=a.id, day=t, shortfall_cents=int(round(s)),
                        explanations=_explain_shortfall(twin, disabled_routes, disabled_lines,
                                                        a.id, t, actions)))

    # ---- exact integer-cent replay under every scenario
    checks = [simulate(twin, s, actions, H) for s in scenarios]
    stress_checks = [StressCheck(scenario_id=s.id, breach=c.metrics.breach,
                                 worst_shortfall_cents=c.metrics.worst_shortfall_cents,
                                 min_headroom_cents=c.metrics.min_headroom_cents,
                                 rejected_actions=len(c.rejected_actions))
                     for s, c in zip(scenarios, checks)]

    if violations:
        status = "infeasible"
    elif any(c.breach or c.rejected_actions for c in stress_checks):
        status = "verification_failed"
    else:
        status = "feasible"

    return Plan(
        id=_plan_id(twin.snapshot_id, scenarios, actions),
        status=status,
        actionable=status == "feasible",
        actions=actions,
        transfer_total_cents=sum(a.amount_cents for a in actions if a.kind == "transfer"),
        credit_draw_total_cents=sum(a.amount_cents for a in actions if a.kind == "credit_draw"),
        modeled_cost_cents=checks[0].metrics.fees_cents,
        violations=violations,
        binding_constraints=_binding_constraints(twin, actions, checks[0].ledger),
        verification=checks[0].metrics,
        stress_checks=stress_checks,
        **common,
    )


def _explain_shortfall(twin: TwinState, disabled_routes: set[str], disabled_lines: set[str],
                       account_id: str, day: int, actions: list[PlanAction]) -> list[str]:
    """Name the funding limits that stop the account from reaching its buffer on `day`."""
    notes: list[str] = []
    for r in twin.routes:
        if r.destination_id != account_id:
            continue
        if not r.allowed:
            notes.append(f"Route {r.id} is not permitted ({r.note or 'policy restriction'}).")
        elif r.id in disabled_routes:
            notes.append(f"Route {r.id} is unavailable in a modeled scenario.")
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
        if not c.enabled or c.id in disabled_lines:
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
    # Accounts with no movement at all (e.g. pledged collateral parked at its buffer)
    # are not "binding"; they would only add noise to the report.
    closings: dict[str, set[int]] = {}
    for rowx in ledger:
        closings.setdefault(rowx.account_id, set()).add(rowx.closing_cents)
        closings[rowx.account_id].add(rowx.opening_cents)
    tight_days: dict[str, list[int]] = {}
    for rowx in ledger:
        if len(closings[rowx.account_id]) == 1:
            continue
        if rowx.closing_cents == rowx.buffer_cents and rowx.buffer_cents > 0:
            tight_days.setdefault(rowx.account_id, []).append(rowx.day)
    for account_id, days in tight_days.items():
        buf = twin.account(account_id).minimum_buffer_cents
        out.append(f"{account_id} held exactly at its {fmt_usd(buf)} buffer from day {days[0]}.")
    return out
