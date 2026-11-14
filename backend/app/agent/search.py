"""Deterministic adaptive risk search (an elite-keeping random + mutation loop; no LLM, no randomness
beyond a seeded numpy Generator).

Each evaluation replays a candidate shock on every *probe* twin (the base twin plus "approved bill X is paid
at its earliest date" twins whose unshocked baseline is clean) and keeps the worst. The score is

    risk_score = signal x plausibility - 0.05 x severity + novelty

with `signal` a squashed 0..1 measure of buffer shortfall cent-days + uncovered mandatory payments, severity
normalised to 0..1, and a small novelty bonus for elites far from the existing ones. The first
`random_prefix` evaluations are random draws, later ones mutate a random elite (20% still random).
The equal-budget random baseline uses the *same* random stream and never mutates.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from pydantic import BaseModel

from ..engine.models import Scenario, TwinState
from ..engine.optimizer import optimize_plan
from ..engine.scenarios import DISCOVERY_SEED_BASE
from ..engine.simulator import simulate
from .shocks import (
    ShockBounds,
    ShockSpace,
    ShockVector,
    distance,
    mutate_within_bounds,
    plausibility,
    random_plausible_shock,
    shock_severity,
    shock_to_scenario,
)

SEVERITY_WEIGHT = 0.05
NOVELTY_WEIGHT = 0.02
MUTATE_SHARE = 0.8


@dataclass(frozen=True)
class Probe:
    id: str
    twin: TwinState


class Eval(BaseModel):
    k: int
    scenario_id: str
    shock: ShockVector
    score: float
    signal: float
    plausibility: float
    severity: float
    breach: bool
    probe: str
    worst_shortfall_cents: int
    first_breach: dict[str, Any] | None = None
    reason: str = "random"


@dataclass
class SearchResult:
    elites: list[Eval]
    trace: list[Eval]
    first_breach_found_at: int | None
    evaluations: int
    seed: int
    budget: int
    unique_breaches: int = 0
    first_unfixable_found_at: int | None = None


def _raw_risk(metrics) -> float:
    """Dollars of buffer shortfall cent-days plus 10x the uncovered mandatory payments."""
    return metrics.shortfall_cent_days / 100 + 10 * metrics.uncovered_mandatory_cents / 100


def _signal(raw: float) -> float:
    return min(1.0, math.log10(1 + raw) / 7) if raw > 0 else 0.0


class Evaluator:
    """Scores shocks against all probes; memoized per shock so repeated draws cost nothing."""

    def __init__(self, probes: list[Probe], space: ShockSpace):
        self.probes, self.space = probes, space
        self._cache: dict[str, tuple[float, float, float, float, bool, str, int, dict | None]] = {}
        b = space.bounds
        self._max_sev = max(1.0, len(space.keys) * (b.receipt_delay_max_days / max(1, b.receipt_delay_max_days) + 1)
                            + 3)

    def scenario(self, shock: ShockVector, k: int) -> Scenario:
        return shock_to_scenario(self.probes[0].twin, shock, f"shock-{k}")

    def evaluate(self, shock: ShockVector, k: int, reason: str) -> Eval:
        key = shock.key()
        if key not in self._cache:
            scen = shock_to_scenario(self.probes[0].twin, shock, "probe")
            best_raw, best_probe, best_short, best_first, breach = -1.0, "", 0, None, False
            for p in self.probes:
                m = simulate(p.twin, scen).metrics
                raw = _raw_risk(m)
                if m.breach and (raw > best_raw or not breach):
                    best_raw, best_probe, best_short = raw, p.id, m.worst_shortfall_cents
                    best_first = m.first_breach.model_dump() if m.first_breach else None
                breach = breach or m.breach
            if not breach:
                best_raw, best_probe = 0.0, self.probes[0].id
            plaus = plausibility(self.probes[0].twin, scen)
            sev = shock_severity(shock, self.space.bounds) / self._max_sev
            self._cache[key] = (_signal(best_raw), plaus, sev, 0.0, breach, best_probe, best_short, best_first)
        signal, plaus, sev, _, breach, probe, short, first = self._cache[key]
        return Eval(k=k, scenario_id=f"shock-{k}", shock=shock, score=signal * plaus - SEVERITY_WEIGHT * sev,
                    signal=signal, plausibility=plaus, severity=sev, breach=breach, probe=probe,
                    worst_shortfall_cents=short, first_breach=first, reason=reason)


def _niche(e: Eval) -> tuple:
    """Structural signature: which route/credit outage and whether a surprise outflow is involved."""
    return (e.shock.disabled_route, e.shock.disabled_credit, e.shock.outflow is not None)


def keep_top_unique(elites: list[Eval], candidate: Eval, capacity: int = 10) -> list[Eval]:
    """Insert `candidate`, keeping distinct shock vectors and at most one elite per structural niche
    (so a pile of near-identical receipt-delay shocks cannot crowd out a route-outage finding)."""
    pool = {e.shock.key(): e for e in elites}
    key = candidate.shock.key()
    if key not in pool or candidate.score > pool[key].score:
        pool[key] = candidate
    best: dict[tuple, Eval] = {}
    for e in sorted(pool.values(), key=lambda e: (-e.score, e.shock.key())):
        best.setdefault(_niche(e), e)
    return sorted(best.values(), key=lambda e: (-e.score, e.shock.key()))[:capacity]


def _novelty(elites: list[Eval], shock: ShockVector) -> float:
    others = [distance(shock, e.shock) for e in elites if e.shock.key() != shock.key()]
    return NOVELTY_WEIGHT * min(others, default=0) / 5.0


def search(probes: list[Probe], bounds: ShockBounds, *, budget: int = 120, seed: int = 0,
           random_prefix: int = 20, capacity: int = 10, adaptive: bool = True,
           evaluator: Evaluator | None = None) -> SearchResult:
    space = ShockSpace.from_twin(probes[0].twin, bounds)
    ev = evaluator or Evaluator(probes, space)
    rng = np.random.default_rng(DISCOVERY_SEED_BASE + seed)      # random stream shared with the baseline
    mut_rng = np.random.default_rng(DISCOVERY_SEED_BASE + seed + 7919)
    elites: list[Eval] = []
    trace: list[Eval] = []
    first_breach: int | None = None
    seen_breach: set[str] = set()
    for k in range(budget):
        drawn = random_plausible_shock(rng, space)                # always consume the shared stream
        parents = [e for e in elites if e.breach]
        if adaptive and k >= random_prefix and parents and float(mut_rng.random()) < MUTATE_SHARE:
            # half the time refine the current best elite, otherwise any elite
            pick = int(mut_rng.integers(0, len(parents)))
            parent = parents[0] if float(mut_rng.random()) < 0.5 else parents[pick]
            covered = {e.shock.disabled_route for e in elites}
            fresh = tuple(r for r in space.routes if r not in covered)
            shock = mutate_within_bounds(parent.shock, mut_rng, space, fresh)
            reason = f"mutate_of={parent.scenario_id}"
        else:
            shock, reason = drawn, "random"
        e = ev.evaluate(shock, k, reason)
        e = e.model_copy(update={"score": e.score + (_novelty(elites, shock) if e.breach else 0.0)})
        trace.append(e)
        if e.breach and e.plausibility > 0:
            seen_breach.add(shock.key())
            if first_breach is None:
                first_breach = k
        elites = keep_top_unique(elites, e, capacity)
    return SearchResult(elites=elites, trace=trace, first_breach_found_at=first_breach, evaluations=len(trace),
                        seed=seed, budget=budget, unique_breaches=len(seen_breach))


@dataclass
class Fixability:
    fixable: bool
    report: list[str] = field(default_factory=list)
    status: str = ""


def assess_fixable(probe_twin: TwinState, scenario: Scenario) -> Fixability:
    """Can any transfer/credit plan keep every buffer under this shock? (`optimize_plan(twin, stress=[...])`)"""
    plan = optimize_plan(probe_twin, stress=[scenario])
    report = [x for v in plan.violations for x in v.explanations]
    return Fixability(plan.status == "feasible", report, plan.status)


def first_unfixable(probes: dict[str, TwinState], trace: list[Eval], ev_scenario, limit: int = 12) -> int | None:
    """Index of the first plausible breaching evaluation whose shock no plan can survive."""
    seen: set[str] = set()
    checked = 0
    for e in trace:
        if not (e.breach and e.plausibility > 0) or e.shock.key() in seen:
            continue
        seen.add(e.shock.key())
        if checked >= limit:
            return None
        checked += 1
        if not assess_fixable(probes[e.probe], ev_scenario(e)).fixable:
            return e.k
    return None
