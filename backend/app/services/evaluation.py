"""Policy evaluation (CASHGUARD_PLAN.md section 6.3, v3 A/B/C protocol).

Setting: a "pay run". Every approved bill is paid on its earliest legal date (the posture an AP team
defaults to), and each policy decides how to FUND it using discovery information only:

  A   rule of thumb: whenever the expected-timeline projection dips below a buffer, top the account up
      from the cheapest route that can arrive in time (then credit); no optimizer, no stress.
  B   LP on the expected timeline only (`optimize_plan(twin)`).
  B+  LP robust to the default stress set (adverse quantile + each customer at its worst delay).
      Ablation: isolates what the risk search adds on top of plain robust planning.
  C   LP robust to the default set + shocks discovered by the adaptive risk search (`agent.search`).

Every plan is frozen and replayed on the SAME held-out paths, which planning never sees:
  * receipt timing from `sample_scenarios` with HELDOUT_SEED_BASE seeds (correlated per customer);
  * structural shocks drawn from an explicit, documented model: each path independently has a
    route outage with probability P_ROUTE_OUTAGE and a surprise outflow with probability P_OUTFLOW.

The search itself is also compared with an equal-budget random baseline (same random stream).
Fixtures: the demo company plus N generated variants (deterministic perturbations of balances, amounts,
receipt dates and customer lateness). All numbers are frequencies under synthetic distributions, not
calibrated probabilities.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import numpy as np

from ..engine.models import (
    CreditDrawAction,
    PlanAction,
    Scenario,
    TransferAction,
    TwinState,
    UnexpectedOutflow,
)
from ..engine.optimizer import optimize_plan
from ..engine.scenarios import HELDOUT_SEED_BASE, sample_scenarios
from ..engine.simulator import simulate
from ..engine.twin import build_twin
from ..agent.search import Probe, assess_fixable, first_unfixable, search
from ..agent.shocks import ShockBounds, shock_to_scenario
from ..services.stress import default_stress_set
from ..workflows.forecast import build_twin_from_store, day_index
from ..workflows.models import APBill
from ..workflows.store import Store

FIXTURE_SEED_BASE = 2_000_000          # generated fixtures; disjoint from discovery (0..) and held-out (1e6..)
HELDOUT_EVAL_OFFSET = 500              # held-out seeds = HELDOUT_SEED_BASE + 500 + fixture index
P_ROUTE_OUTAGE = 0.05
P_OUTFLOW = 0.10
MAX_DISCOVERED = 4

POLICIES: dict[str, str] = {
    "L": "Pay every bill on its latest legal date, no transfers (delay baseline)",
    "A": "Rule of thumb: pay on earliest date, top up any account projected below its buffer",
    "B": "LP on the expected timeline (no stress)",
    "B+": "LP robust to the default stress set (ablation: no search)",
    "C": "LP robust to default set + adaptive risk search discoveries",
}


# ------------------------------------------------------------------ fixtures
@dataclass(frozen=True)
class EvalFixture:
    name: str
    base: TwinState                     # forecast as stored (approved bills on their due dates)
    open_bills: dict[str, int]          # approved bill id -> earliest legal day
    bounds: ShockBounds
    risk_tolerance: float


def _with_obligation_days(twin: TwinState, days: dict[str, int]) -> TwinState:
    fields = {k: getattr(twin, k) for k in TwinState.model_fields if k != "snapshot_id"}
    fields["obligations"] = [o.model_copy(update={"due_day": days[o.id]}) if o.id in days else o
                             for o in twin.obligations]
    return build_twin(**fields)


def fixture_from_store(store: Store, name: str) -> EvalFixture:
    cfg = store.get_config()
    base = build_twin_from_store(store)
    present = {o.id for o in base.obligations}
    open_bills = {b.id: max(0, day_index(cfg, b.earliest_pay_date)) for b in store.list(APBill)
                  if b.status == "approved" and b.id in present}
    return EvalFixture(name, base, dict(sorted(open_bills.items())),
                       ShockBounds.from_config(cfg.shock_bounds), cfg.risk_tolerance)


def _round_cents(x: float, unit: int = 10_000) -> int:
    return max(unit, int(round(x / unit)) * unit)


def generate_variant(fx: EvalFixture, index: int) -> EvalFixture:
    """Deterministic perturbation: balances x[0.85,1.15], payments and receipts x[0.8,1.2], receipt dates
    +-1 day, and per-customer extra lateness (mass w in [0, 0.25] moved to the customer's worst delay)."""
    rng = np.random.default_rng(FIXTURE_SEED_BASE + index)
    t = fx.base
    H = t.horizon_days
    accounts = [a.model_copy(update={"observed_balance_cents": _round_cents(
        a.observed_balance_cents * rng.uniform(0.85, 1.15)) if a.observed_balance_cents > a.minimum_buffer_cents
        else a.observed_balance_cents}) for a in t.accounts]
    obligations = [o.model_copy(update={"amount_cents": _round_cents(o.amount_cents * rng.uniform(0.8, 1.2))})
                   for o in t.obligations]
    keys = sorted({r.correlation_key or r.id for r in t.receipts})
    late_w = {k: float(rng.uniform(0.0, 0.25)) for k in keys}
    receipts = []
    for r in t.receipts:
        k = r.correlation_key or r.id
        worst = max(r.delay_distribution)
        dist = {d: p * (1 - late_w[k]) for d, p in r.delay_distribution.items()}
        dist[worst] = dist.get(worst, 0.0) + late_w[k]
        shift = int(rng.integers(-1, 2)) if not r.id.startswith("posted:") else 0
        receipts.append(r.model_copy(update={
            "amount_cents": _round_cents(r.amount_cents * rng.uniform(0.8, 1.2)),
            "expected_day": min(H - 1, max(0, r.expected_day + shift)),
            "delay_distribution": dist if not r.id.startswith("posted:") else r.delay_distribution}))
    fields = {k: getattr(t, k) for k in TwinState.model_fields if k != "snapshot_id"}
    fields.update(accounts=accounts, obligations=obligations, receipts=receipts,
                  company_name=f"{t.company_name} / variant {index}")
    return EvalFixture(f"{fx.name}-v{index}", build_twin(**fields), fx.open_bills, fx.bounds, fx.risk_tolerance)


# ------------------------------------------------------------------ held-out
def heldout_paths(twin: TwinState, n: int, seed: int, bounds: ShockBounds) -> list[Scenario]:
    """Receipt timing (Monte Carlo) + an explicit structural-shock model. Seeds are held-out only."""
    base = sample_scenarios(twin, n, seed)
    rng = np.random.default_rng(seed + 1)
    routes = sorted(r.id for r in twin.routes if r.allowed)
    spenders = sorted({o.account_id for o in twin.obligations})
    out: list[Scenario] = []
    for k, s in enumerate(base):
        g_route, i_route = float(rng.random()), int(rng.integers(0, max(1, len(routes))))
        g_out, i_acct, day, amt = (float(rng.random()), int(rng.integers(0, max(1, len(spenders)))),
                                   int(rng.integers(0, twin.horizon_days)), float(rng.uniform(0.25, 1.0)))
        disabled = [routes[i_route]] if routes and bounds.route_outage and g_route < P_ROUTE_OUTAGE else []
        outflows = []
        if spenders and bounds.surprise_outflow_max_cents and g_out < P_OUTFLOW:
            outflows = [UnexpectedOutflow(account_id=spenders[i_acct], day=day, description="held-out surprise",
                                          amount_cents=_round_cents(amt * bounds.surprise_outflow_max_cents, 100_000))]
        out.append(s.model_copy(update={"id": f"heldout-{seed}-{k}", "disabled_routes": disabled,
                                        "unexpected_outflows": outflows}))
    return out


# ------------------------------------------------------------------ policies
def rule_of_thumb(twin: TwinState, max_steps: int = 60) -> list[PlanAction]:
    """Policy A. Repeatedly fix the first projected breach on the expected timeline: cheapest allowed route
    that can still arrive in time, limited by its daily cap and the source's cash above buffer; else credit."""
    actions: list[PlanAction] = []
    buffers = {a.id: a.minimum_buffer_cents for a in twin.accounts}
    for _ in range(max_steps):
        res = simulate(twin, None, actions)
        fb = res.metrics.first_breach
        if fb is None:
            break
        closing = {(r.account_id, r.day): r.closing_cents for r in res.ledger}
        need, added = fb.shortfall_cents, False
        for r in sorted((r for r in twin.routes if r.allowed and r.destination_id == fb.account_id),
                        key=lambda r: (r.fee_rate, r.settlement_days, r.id)):
            dispatch = fb.day - r.settlement_days
            if dispatch < 0:
                continue
            used = sum(a.amount_cents for a in actions
                       if isinstance(a, TransferAction) and a.route_id == r.id and a.day == dispatch)
            spare = min(closing[(r.source_id, d)] for d in range(dispatch, twin.horizon_days)) - buffers[r.source_id]
            amount = min(need, r.max_amount_cents - used, spare)
            if amount > 0:
                actions.append(TransferAction(route_id=r.id, day=dispatch, amount_cents=amount))
                added = True
                break
        if not added:
            for c in twin.credit_lines:
                if not c.enabled or c.account_id != fb.account_id or c.draw_delay_days > fb.day:
                    continue
                drawn = sum(a.amount_cents for a in actions
                            if isinstance(a, CreditDrawAction) and a.credit_line_id == c.id)
                amount = min(need, c.available_cents - drawn)
                if amount > 0:
                    actions.append(CreditDrawAction(credit_line_id=c.id, day=fb.day - c.draw_delay_days,
                                                    amount_cents=amount))
                    added = True
                    break
        if not added:
            break          # the rule has nothing left to try; the breach stays
    return actions


@dataclass
class Discovery:
    scenarios: list[Scenario]
    adaptive: dict[str, Any]
    random: dict[str, Any]
    infeasible_example: dict[str, Any] | None
    runtime_ms: float


def discover(fx: EvalFixture, posture: TwinState, *, budget: int, seed: int) -> Discovery:
    """Run the adaptive search (and the equal-budget random baseline) on clean payment postures, then keep
    discovered shocks that a single plan for the pay run can still survive (unfixable ones are findings)."""
    started = time.perf_counter()
    probes = [Probe("base", fx.base)] if not simulate(fx.base).metrics.breach else []
    for bill, day in fx.open_bills.items():
        twin = _with_obligation_days(fx.base, {bill: day})
        if not simulate(twin).metrics.breach:
            probes.append(Probe(f"pay:{bill}", twin))
    empty = {"evaluations": 0, "unique_breaches": 0, "first_breach_found_at": None, "first_unfixable_found_at": None}
    if not probes:
        return Discovery([], empty, dict(empty), None, (time.perf_counter() - started) * 1000)
    adaptive = search(probes, fx.bounds, budget=budget, seed=seed)
    rand = search(probes, fx.bounds, budget=budget, seed=seed, adaptive=False)
    twins = {p.id: p.twin for p in probes}
    scen = lambda e: _scenario(probes, e)  # noqa: E731

    def summary(r) -> dict[str, Any]:
        return {"evaluations": r.evaluations, "unique_breaches": r.unique_breaches,
                "first_breach_found_at": r.first_breach_found_at,
                "first_unfixable_found_at": first_unfixable(twins, r.trace, scen)}

    defaults = default_stress_set(posture, fx.risk_tolerance).scenarios
    kept: list[Scenario] = []
    example = None
    for e in adaptive.elites:
        if not (e.breach and e.plausibility > 0):
            continue
        s = _scenario(probes, e)
        fx_check = assess_fixable(twins[e.probe], s)
        if not fx_check.fixable:
            if example is None:
                example = {"shock": s.description, "probe": e.probe, "plausibility": e.plausibility,
                           "report": fx_check.report[:4]}
            continue
        if len(kept) < MAX_DISCOVERED and optimize_plan(posture, stress=[*defaults, *kept, s]).status == "feasible":
            kept.append(s)
    return Discovery(kept, summary(adaptive), summary(rand), example, (time.perf_counter() - started) * 1000)


def _scenario(probes: list[Probe], e) -> Scenario:
    return shock_to_scenario(probes[0].twin, e.shock, f"shock-{e.k}")


# ------------------------------------------------------------------ scoring
def score(posture: TwinState, actions: Sequence[PlanAction], paths: Sequence[Scenario]) -> dict[str, Any]:
    runs = [simulate(posture, p, actions) for p in paths]
    n = len(runs)
    nominal = simulate(posture, None, actions)
    return {
        "actions": len(actions),
        "modeled_cost_cents": nominal.metrics.fees_cents,
        "nominal_breach": nominal.metrics.breach,
        "breach_rate": round(sum(r.metrics.breach for r in runs) / n, 4),
        "missed_obligation_rate": round(sum(bool(r.metrics.uncovered_obligations) for r in runs) / n, 4),
        "missed_mandatory_rate": round(sum(bool(r.metrics.uncovered_mandatory_cents) for r in runs) / n, 4),
        "mean_shortfall_dollar_days": round(sum(r.metrics.shortfall_cent_days for r in runs) / n / 100, 2),
        "worst_shortfall_cents": max(r.metrics.worst_shortfall_cents for r in runs),
    }


def evaluate_fixture(fx: EvalFixture, index: int, *, paths: int, budget: int) -> dict[str, Any]:
    posture = _with_obligation_days(fx.base, fx.open_bills)
    heldout_seed = HELDOUT_SEED_BASE + HELDOUT_EVAL_OFFSET + index
    held = heldout_paths(posture, paths, heldout_seed, fx.bounds)
    disc = discover(fx, posture, budget=budget, seed=index)
    defaults = default_stress_set(posture, fx.risk_tolerance).scenarios

    planners: dict[str, Callable[[], tuple[list[PlanAction], str]]] = {
        "A": lambda: (rule_of_thumb(posture), "heuristic"),
        "B": lambda: _lp(posture, []),
        "B+": lambda: _lp(posture, defaults),
        "C": lambda: _lp(posture, [*defaults, *disc.scenarios]),
    }
    policies: dict[str, Any] = {}
    # L: bills stay on their stored due dates (latest legal day), no actions. Held-out scenarios are
    # shock specs (receipt delays, route outages, outflows by account/day), independent of the
    # obligation timeline, so the same `held` paths are reused and results stay comparable.
    t0 = time.perf_counter()
    policies["L"] = {**score(fx.base, [], held), "plan_status": "none",
                     "runtime_ms": round((time.perf_counter() - t0) * 1000, 1)}
    for name, plan in planners.items():
        t0 = time.perf_counter()
        actions, status = plan()
        ms = (time.perf_counter() - t0) * 1000 + (disc.runtime_ms if name == "C" else 0.0)
        policies[name] = {**score(posture, actions, held), "plan_status": status, "runtime_ms": round(ms, 1)}
    return {"fixture": fx.name, "open_bills": fx.open_bills, "heldout_seed": heldout_seed, "search_seed": index,
            "stress_scenarios": {"B+": len(defaults), "C": len(defaults) + len(disc.scenarios)},
            "discovered": [s.description for s in disc.scenarios], "policies": policies,
            "search": {"adaptive": disc.adaptive, "random": disc.random},
            "infeasible_example": disc.infeasible_example}


def _lp(posture: TwinState, stress: list[Scenario]) -> tuple[list[PlanAction], str]:
    plan = optimize_plan(posture, stress=stress)
    return list(plan.actions), plan.status


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in POLICIES:
        ps = [r["policies"][name] for r in rows]
        out[name] = {
            "description": POLICIES[name],
            "mean_breach_rate": round(float(np.mean([p["breach_rate"] for p in ps])), 4),
            "mean_missed_obligation_rate": round(float(np.mean([p["missed_obligation_rate"] for p in ps])), 4),
            "mean_missed_mandatory_rate": round(float(np.mean([p["missed_mandatory_rate"] for p in ps])), 4),
            "total_modeled_cost_cents": int(sum(p["modeled_cost_cents"] for p in ps)),
            "worst_shortfall_cents": int(max(p["worst_shortfall_cents"] for p in ps)),
            "nominal_breaches": int(sum(p["nominal_breach"] for p in ps)),
            "mean_runtime_ms": round(float(np.mean([p["runtime_ms"] for p in ps])), 1),
        }
    def tot(side: str, key: str) -> int:
        return int(sum(r["search"][side][key] or 0 for r in rows))

    def found(side: str, key: str) -> int:
        return sum(r["search"][side][key] is not None for r in rows)

    search_summary = {side: {"evaluations": tot(side, "evaluations"),
                             "unique_breaches": tot(side, "unique_breaches"),
                             "fixtures_with_breach_found": found(side, "first_breach_found_at"),
                             "fixtures_with_unfixable_found": found(side, "first_unfixable_found_at")}
                      for side in ("adaptive", "random")}
    return {"policies": out, "search": search_summary}


def run_evaluation(base: EvalFixture, *, variants: int = 10, paths: int = 200, budget: int = 120,
                   progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    fixtures = [base] + [generate_variant(base, i) for i in range(1, variants + 1)]
    rows = []
    for i, fx in enumerate(fixtures):
        rows.append(evaluate_fixture(fx, i, paths=paths, budget=budget))
        if progress:
            p = rows[-1]["policies"]
            progress(f"{fx.name}: " + "  ".join(f"{k} {v['breach_rate']:.0%}/${v['modeled_cost_cents'] / 100:,.0f}"
                                               for k, v in p.items()))
    return {
        "protocol": {
            "setting": ("pay run: policies A, B, B+, C pay every approved bill on its earliest legal date and choose "
                        "the funding. L is a baseline that instead pays every bill on its latest legal date with no "
                        "transfers: delaying is a valid baseline only when there is no early-payment discount and no "
                        "vendor late penalty, and it cannot stop NEW spend before it is committed, which is "
                        "Buffer's main value"),
            "fixtures": len(fixtures), "generated_variants": variants, "heldout_paths_per_fixture": paths,
            "search_budget": budget, "seeds": {"fixture_base": FIXTURE_SEED_BASE,
                                              "heldout_base": HELDOUT_SEED_BASE + HELDOUT_EVAL_OFFSET,
                                              "search": "fixture index (DISCOVERY_SEED_BASE + i)"},
            "heldout_shock_model": {"receipt_timing": "Monte Carlo from customer delay distributions (correlated)",
                                    "p_route_outage": P_ROUTE_OUTAGE, "p_surprise_outflow": P_OUTFLOW,
                                    "outflow_max_cents": base.bounds.surprise_outflow_max_cents},
            "policies": POLICIES,
            "caveat": "Frequencies under synthetic distributions and a documented shock model; not calibrated.",
        },
        "summary": summarize(rows),
        "fixtures": rows,
        "runtime_s": round(time.perf_counter() - started, 1),
    }
