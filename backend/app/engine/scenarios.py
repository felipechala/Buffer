"""Scenario generation: Monte Carlo sampling of receipt timing and named stress cases.

Correlation model (deliberately simple and explicit): receipts sharing a
`correlation_key` share one uniform draw, so they are delayed together
(comonotone). Receipts without a key are sampled independently.

Breach frequencies computed from these samples are conditional on the synthetic
delay distributions. They are not calibrated real-world probabilities.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .models import PlanAction, Scenario, TwinState
from .simulator import simulate

# Seed groups: discovery/planning and held-out evaluation must never overlap.
DISCOVERY_SEED_BASE = 0
HELDOUT_SEED_BASE = 1_000_000


def _inverse_cdf(distribution: dict[int, float], u: float) -> int:
    acc = 0.0
    for delay in sorted(distribution):
        acc += distribution[delay]
        if u < acc:
            return delay
    return max(distribution)


def sample_scenarios(twin: TwinState, n: int, seed: int) -> list[Scenario]:
    rng = np.random.default_rng(seed)
    keys = sorted({r.correlation_key for r in twin.receipts if r.correlation_key})
    out: list[Scenario] = []
    for k in range(n):
        shared = {key: float(rng.random()) for key in keys}
        delays: dict[str, int] = {}
        for r in twin.receipts:
            u = shared[r.correlation_key] if r.correlation_key else float(rng.random())
            d = _inverse_cdf(r.delay_distribution, u)
            if d:
                delays[r.id] = d
        out.append(Scenario(id=f"mc-{seed}-{k}", receipt_delays=delays, seed=seed,
                            description="Monte Carlo receipt timing sample"))
    return out


def adverse_scenario(twin: TwinState, quantile: float) -> Scenario:
    """Every receipt at the given quantile of its delay distribution, simultaneously.

    Conservative by construction: it assumes all customers are late together.
    Because a later receipt never increases any balance, a plan that survives this
    scenario survives every sample whose delays are all at or below these quantiles.
    """
    delays = {r.id: d for r in twin.receipts
              if (d := _inverse_cdf(r.delay_distribution, quantile))}
    return Scenario(id=f"adverse-q{round(quantile * 100)}", receipt_delays=delays,
                    description=f"all receipts at their {quantile:.0%} delay quantile")


def scenario_probability(twin: TwinState, scenario: Scenario) -> float:
    """Plausibility weight of the receipt-delay part of a scenario under the twin's
    distributions (correlated receipts counted once per key). 0 if impossible."""
    p = 1.0
    seen_keys: set[str] = set()
    for r in twin.receipts:
        d = scenario.receipt_delays.get(r.id, 0)
        prob = sum(v for k, v in r.delay_distribution.items() if k >= d) if d else 1.0
        if r.correlation_key:
            if r.correlation_key in seen_keys:
                continue
            seen_keys.add(r.correlation_key)
        p *= prob
    return p


@dataclass(frozen=True)
class MonteCarloSummary:
    n: int
    seed: int
    breach_rate: float
    missed_obligation_rate: float
    mean_shortfall_cent_days: float
    worst_shortfall_cents: int
    p5_min_headroom_cents: int


def evaluate_monte_carlo(twin: TwinState, actions: Sequence[PlanAction],
                         scenarios: Sequence[Scenario]) -> MonteCarloSummary:
    results = [simulate(twin, s, actions) for s in scenarios]
    n = len(results)
    headroom = sorted(r.metrics.min_headroom_cents for r in results)
    return MonteCarloSummary(
        n=n,
        seed=scenarios[0].seed if scenarios and scenarios[0].seed is not None else -1,
        breach_rate=sum(r.metrics.breach for r in results) / n,
        missed_obligation_rate=sum(bool(r.metrics.uncovered_mandatory_cents) for r in results) / n,
        mean_shortfall_cent_days=sum(r.metrics.shortfall_cent_days for r in results) / n,
        worst_shortfall_cents=max(r.metrics.worst_shortfall_cents for r in results),
        p5_min_headroom_cents=headroom[int(0.05 * (n - 1))],
    )
