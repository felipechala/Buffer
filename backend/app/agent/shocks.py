"""Shock library for the adaptive risk search.

A `ShockVector` is a compact, bounded description of "what goes wrong": customers paying late (one delay
per correlation key, so correlated receipts move together), a collection haircut, one surprise outflow,
at most one route outage and at most one credit-line outage. Bounds come from the treasury overlay
(`CompanyConfig.shock_bounds`). Everything here is pure and driven by an explicit numpy Generator.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np
from pydantic import BaseModel, Field

from ..engine.models import Scenario, TwinState, UnexpectedOutflow, fmt_usd
from ..engine.scenarios import scenario_probability


class ShockBounds(BaseModel):
    receipt_delay_max_days: int = Field(default=5, ge=0, le=30)
    collection_fraction_min: float = Field(default=0.7, gt=0, le=1)     # at worst this share is collected
    surprise_outflow_max_cents: int = Field(default=2_000_000, ge=0)
    route_outage: bool = True
    credit_outage: bool = True

    @classmethod
    def from_config(cls, raw: dict) -> "ShockBounds":
        return cls.model_validate({k: v for k, v in (raw or {}).items() if k in cls.model_fields})


class ShockVector(BaseModel):
    delays: dict[str, int] = {}              # correlation key (or receipt id) -> extra delay days
    haircuts: dict[str, float] = {}          # key -> fraction NOT collected
    outflow: UnexpectedOutflow | None = None
    disabled_route: str | None = None
    disabled_credit: str | None = None

    def key(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ShockSpace:
    """What a shock may touch in a given twin."""
    keys: tuple[str, ...]
    routes: tuple[str, ...]
    credit_lines: tuple[str, ...]
    accounts: tuple[str, ...]
    horizon: int
    bounds: ShockBounds
    max_delay: dict[str, int] = field(default_factory=dict)   # largest delay each key's distribution allows

    @classmethod
    def from_twin(cls, twin: TwinState, bounds: ShockBounds) -> "ShockSpace":
        keys = sorted({r.correlation_key or r.id for r in twin.receipts})
        maxd: dict[str, int] = {}
        for r in twin.receipts:
            k = r.correlation_key or r.id
            maxd[k] = max(maxd.get(k, 0), max(r.delay_distribution))
        return cls(tuple(keys), tuple(sorted(r.id for r in twin.routes if r.allowed)),
                   tuple(sorted(c.id for c in twin.credit_lines)), tuple(sorted(a.id for a in twin.accounts)),
                   twin.horizon_days, bounds, maxd)


def shock_to_scenario(twin: TwinState, shock: ShockVector, scenario_id: str, description: str = "") -> Scenario:
    delays: dict[str, int] = {}
    haircuts: dict[str, float] = {}
    for r in twin.receipts:
        k = r.correlation_key or r.id
        if shock.delays.get(k):
            delays[r.id] = shock.delays[k]
        if shock.haircuts.get(k):
            haircuts[r.id] = shock.haircuts[k]
    return Scenario(
        id=scenario_id, description=description or describe(twin, shock), receipt_delays=delays,
        receipt_haircuts=haircuts, unexpected_outflows=[shock.outflow] if shock.outflow else [],
        disabled_routes=[shock.disabled_route] if shock.disabled_route else [],
        disabled_credit_lines=[shock.disabled_credit] if shock.disabled_credit else [])


def describe(twin: TwinState, shock: ShockVector) -> str:
    names = {(r.correlation_key or r.id): (r.counterparty or r.id) for r in twin.receipts}
    parts = [f"{names.get(k, k)} pays {d}d late" for k, d in sorted(shock.delays.items()) if d]
    parts += [f"{names.get(k, k)} collects {1 - h:.0%}" for k, h in sorted(shock.haircuts.items()) if h]
    if shock.outflow:
        parts.append(f"surprise {fmt_usd(shock.outflow.amount_cents)} outflow from "
                     f"{shock.outflow.account_id} on day {shock.outflow.day}")
    if shock.disabled_route:
        parts.append(f"{shock.disabled_route} route down")
    if shock.disabled_credit:
        parts.append(f"{shock.disabled_credit} credit unavailable")
    return "; ".join(parts) or "no shock"


def shock_severity(shock: ShockVector, bounds: ShockBounds) -> float:
    """Unitless magnitude: each component contributes up to ~1 of its own bound."""
    sev = sum(d / max(1, bounds.receipt_delay_max_days) for d in shock.delays.values())
    spread = max(1e-9, 1 - bounds.collection_fraction_min)
    sev += sum(h / spread for h in shock.haircuts.values())
    if shock.outflow:
        sev += shock.outflow.amount_cents / max(1, bounds.surprise_outflow_max_cents)
    sev += (shock.disabled_route is not None) + (shock.disabled_credit is not None)
    return sev


def plausibility(twin: TwinState, scenario: Scenario) -> float:
    """Receipt-delay probability under the twin's distributions x simple priors for structural shocks."""
    p = scenario_probability(twin, scenario)
    p *= 0.1 ** (len(scenario.disabled_routes) + len(scenario.disabled_credit_lines))
    p *= 0.2 ** len(scenario.unexpected_outflows)
    p *= 0.5 ** len(scenario.receipt_haircuts)
    return p


def distance(a: ShockVector, b: ShockVector) -> int:
    """Number of components that differ (used for the novelty bonus)."""
    d = sum(a.delays.get(k, 0) != b.delays.get(k, 0) for k in set(a.delays) | set(b.delays))
    d += sum(a.haircuts.get(k, 0) != b.haircuts.get(k, 0) for k in set(a.haircuts) | set(b.haircuts))
    d += (a.outflow != b.outflow) + (a.disabled_route != b.disabled_route) + (a.disabled_credit != b.disabled_credit)
    return int(d)


def random_plausible_shock(rng: np.random.Generator, space: ShockSpace) -> ShockVector:
    """Draw a bounded shock. The same number of draws is consumed whatever is chosen, so streams stay aligned."""
    b = space.bounds
    delay_draws = [(float(rng.random()), int(rng.integers(1, max(2, b.receipt_delay_max_days + 1)))) for _ in space.keys]
    hair_gate, hair_key, hair_val = float(rng.random()), int(rng.integers(0, max(1, len(space.keys)))), float(rng.random())
    out_gate, out_acct, out_day, out_amt = (float(rng.random()), int(rng.integers(0, len(space.accounts))),
                                            int(rng.integers(0, space.horizon)), float(rng.random()))
    route_gate, route_i = float(rng.random()), int(rng.integers(0, max(1, len(space.routes))))
    credit_gate, credit_i = float(rng.random()), int(rng.integers(0, max(1, len(space.credit_lines))))

    # random draws stay inside what each customer's history allows; mutation may step past it (implausible)
    delays = {k: min(d, space.max_delay.get(k, 0)) for k, (g, d) in zip(space.keys, delay_draws)
              if g < 0.4 and b.receipt_delay_max_days > 0 and space.max_delay.get(k, 0) > 0}
    haircuts: dict[str, float] = {}
    spread = 1 - b.collection_fraction_min
    if hair_gate < 0.1 and space.keys and spread > 0:
        haircuts[space.keys[hair_key]] = round(0.05 + hair_val * (spread - 0.05), 2) if spread > 0.05 else round(spread, 2)
    outflow = None
    if out_gate < 0.2 and b.surprise_outflow_max_cents > 0:
        cents = max(100_000, int(out_amt * b.surprise_outflow_max_cents) // 100_000 * 100_000)
        outflow = UnexpectedOutflow(account_id=space.accounts[out_acct], day=out_day, amount_cents=cents,
                                    description="surprise outflow")
    route = space.routes[route_i] if b.route_outage and space.routes and route_gate < 0.4 else None
    credit = space.credit_lines[credit_i] if b.credit_outage and space.credit_lines and credit_gate < 0.25 else None
    return ShockVector(delays=delays, haircuts=haircuts, outflow=outflow, disabled_route=route, disabled_credit=credit)


def mutate_within_bounds(shock: ShockVector, rng: np.random.Generator, space: ShockSpace,
                         unexplored_routes: tuple[str, ...] = ()) -> ShockVector:
    """Change one component (sometimes two) by a small step, staying inside the bounds.
    Route-outage moves prefer `unexplored_routes` (routes no current elite already covers)."""
    out = _mutate_once(shock, rng, space, unexplored_routes)
    if float(rng.random()) < 0.5:
        out = _mutate_once(out, rng, space, unexplored_routes)
    return out


def _mutate_once(shock: ShockVector, rng: np.random.Generator, space: ShockSpace,
                 unexplored_routes: tuple[str, ...] = ()) -> ShockVector:
    b = space.bounds
    gate = float(rng.random())
    which = 0 if gate < 0.3 else 1 if gate < 0.6 else 2 if gate < 0.7 else 3 if gate < 0.85 else 4
    step = int(rng.choice([-1, 1]))
    pick = float(rng.random())
    out = shock.model_copy(deep=True)
    if which == 0 and space.keys and b.receipt_delay_max_days > 0:          # nudge one customer's delay
        k = space.keys[int(pick * len(space.keys)) % len(space.keys)]
        d = min(b.receipt_delay_max_days, max(0, out.delays.get(k, 0) + step))
        if d:
            out.delays[k] = d
        else:
            out.delays.pop(k, None)
    elif which == 1 and b.route_outage and space.routes:                    # swap / toggle the route outage
        if unexplored_routes:
            out.disabled_route = unexplored_routes[int(pick * len(unexplored_routes)) % len(unexplored_routes)]
        else:
            idx = int(pick * (len(space.routes) + 1))
            out.disabled_route = None if idx >= len(space.routes) else space.routes[idx]
    elif which == 2 and b.credit_outage and space.credit_lines:
        out.disabled_credit = None if out.disabled_credit else space.credit_lines[int(pick * len(space.credit_lines)) %
                                                                                len(space.credit_lines)]
    elif which == 3 and space.keys and 1 - b.collection_fraction_min > 0:   # nudge a collection haircut
        k = space.keys[int(pick * len(space.keys)) % len(space.keys)]
        h = round(min(1 - b.collection_fraction_min, max(0.0, out.haircuts.get(k, 0.0) + step * 0.05)), 2)
        if h:
            out.haircuts[k] = h
        else:
            out.haircuts.pop(k, None)
    else:                                                                   # toggle / scale the surprise outflow
        if out.outflow is None and b.surprise_outflow_max_cents > 0:
            out.outflow = UnexpectedOutflow(account_id=space.accounts[int(pick * len(space.accounts)) % len(space.accounts)],
                                            day=int(rng.integers(0, space.horizon)),
                                            amount_cents=max(100_000, b.surprise_outflow_max_cents // 2),
                                            description="surprise outflow")
        elif out.outflow is not None:
            cents = out.outflow.amount_cents + step * 500_000
            out.outflow = None if cents <= 0 else out.outflow.model_copy(
                update={"amount_cents": min(b.surprise_outflow_max_cents, cents)})
    return out
