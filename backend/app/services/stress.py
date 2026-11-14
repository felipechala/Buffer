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


class StressSet(BaseModel):
    id: str
    snapshot_id: str
    source: Literal["default", "risk_search"]
    scenarios: list[Scenario]


def default_stress_set(twin: TwinState, risk_tolerance: float) -> StressSet:
    scenarios = [adverse_scenario(twin, 1 - risk_tolerance)]
    by_key: dict[str, list] = {}
    for r in twin.receipts:
        by_key.setdefault(r.correlation_key or r.id, []).append(r)
    for key, receipts in sorted(by_key.items()):
        worst = max(d for r in receipts for d in r.delay_distribution)
        if worst == 0:
            continue
        name = receipts[0].counterparty or key
        scenarios.append(Scenario(
            id=f"{key}-late-{worst}d",
            receipt_delays={r.id: worst for r in receipts},
            description=f"{name} pays {worst} day(s) late"))
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
