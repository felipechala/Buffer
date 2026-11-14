"""The stress set every CashGuard check is replayed against.

Two sources:
  * single shocks derived from the twin (each customer paying d days late, comonotone
    across that customer's invoices), kept when their plausibility under the
    customer's delay distribution is at least `min_plausibility`;
  * shocks discovered by the risk-search agent for this snapshot (cached by snapshot
    id, so a hero call never waits on the search).

Plausibility is a weight under synthetic distributions, not a calibrated probability.
"""
from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel

from ..engine.models import Scenario, TwinState
from ..engine.scenarios import scenario_probability

DEFAULT_MIN_PLAUSIBILITY = 0.10


class StressScenario(BaseModel):
    scenario: Scenario
    label: str
    plausibility: float
    family: Literal["receipt_delay", "route_outage", "credit_unavailable",
                    "surprise_outflow", "haircut", "compound"]
    source: Literal["single_shock", "discovered", "request"] = "single_shock"


class StressCache(BaseModel):
    """Discovered shocks for one snapshot (written by the risk-search agent)."""
    id: str                      # = snapshot_id
    scenarios: list[StressScenario]


def merge_scenarios(base: Scenario, shock: Scenario) -> Scenario:
    """Apply `shock` on top of `base` (e.g. a judge's override): later delays win per
    receipt, haircuts take the max, outflows add up, outages union."""
    if base.id == "baseline" and not base.receipt_delays and not base.disabled_routes \
            and not base.disabled_credit_lines and not base.unexpected_outflows \
            and not base.receipt_haircuts:
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


def receipt_delay_shocks(twin: TwinState, min_plausibility: float) -> list[StressScenario]:
    groups: dict[str, list] = {}
    for r in twin.receipts:
        if r.id.startswith("posted:"):
            continue   # posted future-dated cash is certain, not a forecast
        groups.setdefault(r.correlation_key or r.id, []).append(r)
    out: list[StressScenario] = []
    for key, receipts in sorted(groups.items()):
        delays = sorted({d for r in receipts for d in r.delay_distribution if d > 0})
        for d in delays:
            sc = Scenario(id=f"late:{key}:{d}d", receipt_delays={r.id: d for r in receipts},
                          description=f"{key} receipts delayed {d} day(s)")
            p = scenario_probability(twin, sc)
            if p + 1e-9 < min_plausibility:
                continue
            who = receipts[0].counterparty or key
            ids = ", ".join(r.id for r in receipts)
            label = (f"{who} invoice {ids} arrives {_plural(d, 'day')} late" if len(receipts) == 1
                     else f"{who} pays {_plural(d, 'day')} late ({ids})")
            out.append(StressScenario(scenario=sc, label=label, plausibility=round(p, 4),
                                      family="receipt_delay"))
    return out


def stress_set(twin: TwinState, discovered: list[StressScenario] | None = None,
               min_plausibility: float = DEFAULT_MIN_PLAUSIBILITY) -> list[StressScenario]:
    singles = receipt_delay_shocks(twin, min_plausibility)
    seen = {s.scenario.id for s in singles}
    extra = [s for s in (discovered or []) if s.scenario.id not in seen
             and s.plausibility + 1e-9 >= min_plausibility]
    return singles + extra


def stress_set_id(scenarios: list[StressScenario]) -> str:
    digest = hashlib.sha256("|".join(sorted(s.scenario.id for s in scenarios)).encode()).hexdigest()
    return f"stress_{digest[:8]} ({len(scenarios)} scenarios)"
