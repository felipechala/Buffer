"""Stress set: the scenarios every decision is checked against, cached per base snapshot.

Until the risk-search agent has run for a snapshot, the default set is the adverse
scenario plus one "customer late" scenario per correlated customer at the worst delay in
its distribution. The agent replaces the set with discovered shocks (see agent/).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from ..engine.models import Scenario, TwinState
from ..engine.scenarios import adverse_scenario, scenario_probability

_CACHE: dict[str, "StressSet"] = {}

# Discovered shocks below this plausibility are findings (the danger list) but never constrain decisions.
# Override per company with config.shock_bounds["min_plausibility"].
DEFAULT_MIN_PLAUSIBILITY = 0.10


class StressSet(BaseModel):
    id: str
    snapshot_id: str
    source: Literal["default", "risk_search"]
    scenarios: list[Scenario]


def merge_scenarios(base: Scenario, shock: Scenario) -> Scenario:
    """Apply `shock` on top of `base`: later delays win per receipt, haircuts take the
    max, outflows add up, outages union."""
    if base.id == "baseline" and not base.receipt_delays and not base.disabled_routes             and not base.disabled_credit_lines and not base.unexpected_outflows             and not base.receipt_haircuts:
        return shock
    delays = dict(base.receipt_delays)
    for k, v in shock.receipt_delays.items():
        delays[k] = max(v, delays.get(k, 0))
    cuts = dict(base.receipt_haircuts)
    for k, v in shock.receipt_haircuts.items():
        cuts[k] = max(v, cuts.get(k, 0.0))
    return Scenario(
        id=f"{base.id}+{shock.id}",
        description="; ".join(x for x in (base.description, shock.description) if x),
        receipt_delays=delays, receipt_haircuts=cuts,
        unexpected_outflows=[*base.unexpected_outflows, *shock.unexpected_outflows],
        disabled_routes=sorted({*base.disabled_routes, *shock.disabled_routes}),
        disabled_credit_lines=sorted({*base.disabled_credit_lines, *shock.disabled_credit_lines}),
        seed=shock.seed if shock.seed is not None else base.seed)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def default_stress_set(twin: TwinState, risk_tolerance: float) -> StressSet:
    scenarios = [adverse_scenario(twin, 1 - risk_tolerance)]
    by_key: dict[str, list] = {}
    for r in twin.receipts:
        if r.id.startswith("posted:"):
            continue   # posted future-dated cash is certain, not a forecast
        by_key.setdefault(r.correlation_key or r.id, []).append(r)
    for key, receipts in sorted(by_key.items()):
        worst = max(d for r in receipts for d in r.delay_distribution)
        if worst == 0:
            continue
        name = receipts[0].counterparty or key
        ids = ", ".join(r.id for r in receipts)
        label = (f"{name} invoice {ids} arrives {_plural(worst, 'day')} late" if len(receipts) == 1
                 else f"{name} pays {_plural(worst, 'day')} late ({ids})")
        scenarios.append(Scenario(
            id=f"{key}-late-{worst}d",
            receipt_delays={r.id: worst for r in receipts},
            description=label))
    return StressSet(id=f"stress-default-{twin.snapshot_id}", snapshot_id=twin.snapshot_id,
                     source="default", scenarios=scenarios)


def get_stress_set(twin: TwinState, risk_tolerance: float) -> StressSet:
    cached = _CACHE.get(twin.snapshot_id)
    if cached is None:
        cached = default_stress_set(twin, risk_tolerance)
        _CACHE[twin.snapshot_id] = cached
    return cached


def set_stress_set(stress: StressSet) -> None:
    _CACHE[stress.snapshot_id] = stress


def plausibility(twin: TwinState, scenario: Scenario) -> float:
    """Receipt-delay probability, discounted for structural shocks (route/credit outage)."""
    p = scenario_probability(twin, scenario)
    p *= 0.1 ** (len(scenario.disabled_routes) + len(scenario.disabled_credit_lines))
    p *= 0.2 ** len(scenario.unexpected_outflows)
    return p
