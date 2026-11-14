"""Collection history -> per-customer on-time share and empirical delay distribution.

Input is completed deposits whose description carries `INV-... <customer> due YYYY-MM-DD`
(the convention seed_nessie.py and the normalizer share). Early payments count as 0 days late.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel

from ..workflows.models import Customer


@dataclass(frozen=True)
class PaymentObservation:
    customer_id: str
    invoice_id: str
    due: date
    paid: date

    @property
    def days_late(self) -> int:
        return max(0, (self.paid - self.due).days)


class CustomerHistory(BaseModel):
    customer_id: str
    samples: int
    on_time_share: float
    delay_distribution: dict[int, float]


def build_history(observations: list[PaymentObservation]) -> dict[str, CustomerHistory]:
    by_customer: dict[str, list[PaymentObservation]] = {}
    for o in observations:
        by_customer.setdefault(o.customer_id, []).append(o)
    out: dict[str, CustomerHistory] = {}
    for cid, obs in by_customer.items():
        counts = Counter(o.days_late for o in obs)
        n = len(obs)
        dist = {d: round(c / n, 6) for d, c in sorted(counts.items())}
        # make the distribution sum to exactly 1 (the engine validates 1e-6) by adjusting the largest bucket
        drift = round(1.0 - sum(dist.values()), 6)
        if drift:
            top = max(dist, key=lambda k: dist[k])
            dist[top] = round(dist[top] + drift, 6)
        out[cid] = CustomerHistory(customer_id=cid, samples=n, on_time_share=round(counts.get(0, 0) / n, 6),
                                   delay_distribution=dist)
    return out


def apply_history(customers: list[Customer], history: dict[str, CustomerHistory],
                  min_samples: int = 3) -> list[Customer]:
    """Override a customer's synthetic delay distribution only when there are enough observations."""
    out = []
    for c in customers:
        h = history.get(c.id)
        out.append(c.model_copy(update={"delay_distribution": h.delay_distribution})
                   if h and h.samples >= min_samples else c)
    return out
