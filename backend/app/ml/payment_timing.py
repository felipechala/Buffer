"""Learned payment-timing model: P(days late) per open invoice.

Why: the twin's receipt timing comes from a per-customer histogram (`Customer.delay_distribution`), which ignores
everything about the invoice itself. A small gradient-boosted classifier conditions on invoice features instead.

Data, honestly: the demo company has only a handful of paid invoices. Training uses
  * every real paid invoice in the store (fixture or Nessie history; due date vs paid date), plus
  * a SYNTHETIC collection history (`synthetic_history`), generated per customer from a fixed, documented rule:
    the customer's base lateness, more lateness on invoices large relative to that customer, at month-end and at
    quarter-end. It stands in for the deposit history a real company would have. Nothing here is a real customer.
The model card reports holdout log-loss against the per-customer histogram so the gain is measured, not claimed.

Output is advisory (an agent tool and an API); the engine's stress set and Monte Carlo keep using the documented
distributions, so decisions stay reproducible.
"""
from __future__ import annotations

import math
import threading
from datetime import date, timedelta
from typing import Any

import numpy as np
from pydantic import BaseModel

from ..workflows.models import ARInvoice, Customer
from ..workflows.store import Store

BUCKETS = (0, 1, 2, 3)          # days late; 3 = "3 or more"
SYNTH_PER_CUSTOMER = 240
SEED = 7


class ModelCard(BaseModel):
    trained_on_real: int
    trained_on_synthetic: int
    holdout_rows: int
    holdout_log_loss_model: float
    holdout_log_loss_histogram: float
    improvement_pct: float
    features: list[str]
    algorithm: str
    note: str


class Prediction(BaseModel):
    invoice_id: str
    customer: str
    amount_cents: int
    due_date: date
    p_late: float                       # P(>= 1 day late)
    p_late_3plus: float
    distribution: dict[int, float]      # model
    assumed_distribution: dict[int, float]   # what the twin uses (per-customer histogram)
    expected_days_late: float


def _bucket(days: int) -> int:
    return min(max(days, 0), 3)


def _features(c: Customer, amount_cents: int, due: date, mean_amount: float) -> list[float]:
    base_late = 1.0 - c.delay_distribution.get(0, c.delay_distribution.get("0", 1.0))  # type: ignore[arg-type]
    nxt = due + timedelta(days=1)
    month_end = 1.0 if nxt.month != due.month or due.day >= 28 else 0.0
    quarter_end = 1.0 if month_end and due.month in (3, 6, 9, 12) else 0.0
    return [base_late, math.log(amount_cents / 100.0), amount_cents / max(mean_amount, 1.0), month_end, quarter_end,
            float(due.weekday() >= 4), float(c.payment_terms_days)]


FEATURES = ["customer_base_lateness", "log_amount", "amount_vs_customer_mean", "month_end", "quarter_end",
            "due_friday_or_weekend", "payment_terms_days"]


def _dist(c: Customer) -> dict[int, float]:
    d: dict[int, float] = {b: 0.0 for b in BUCKETS}
    for k, v in c.delay_distribution.items():
        d[_bucket(int(k))] += float(v)
    return d


def synthetic_history(customers: list[Customer], rng: np.random.Generator) -> list[tuple[Customer, int, date, int]]:
    """(customer, amount_cents, due_date, days_late) from the documented generating rule."""
    rows = []
    for c in customers:
        base = _dist(c)
        late0 = 1.0 - base[0]
        mean_amt = 100_000.0 * (1 + sum(map(ord, c.id)) % 3)   # stable across processes
        for _ in range(SYNTH_PER_CUSTOMER):
            amt = float(rng.lognormal(math.log(mean_amt), 0.5))
            due = date(2025, 1, 1) + timedelta(days=int(rng.integers(0, 680)))
            month_end = (due + timedelta(days=1)).month != due.month or due.day >= 28
            quarter_end = month_end and due.month in (3, 6, 9, 12)
            ratio = min(amt / mean_amt, 3.0)
            p_late = min(0.95, late0 * (0.5 + 0.5 * ratio) + (0.18 if month_end else 0.0)
                         + (0.15 if quarter_end else 0.0))
            if rng.random() >= p_late:
                days = 0
            else:   # severity follows the customer's own late buckets, shifted later for large invoices
                tail = np.array([base[1], base[2], base[3]]) + 1e-3
                if amt > 1.5 * mean_amt:
                    tail = tail * np.array([0.6, 1.0, 1.8])
                days = int(rng.choice([1, 2, 3], p=tail / tail.sum()))
            rows.append((c, int(round(amt * 100)), due, days))
    return rows


def _real_history(store: Store, customers: dict[str, Customer]) -> list[tuple[Customer, int, date, int]]:
    rows = []
    for inv in store.list(ARInvoice):
        c = customers.get(inv.customer_id)
        if c is None or inv.status != "paid":
            continue
        paid = inv.paid_date or inv.expected_receipt_date
        rows.append((c, inv.amount_cents, inv.due_date, max(0, (paid - inv.due_date).days)))
    return rows


def _log_loss(probs: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(probs[np.arange(len(y)), y], 1e-6, 1.0)
    return float(-np.mean(np.log(p)))


class PaymentTimingModel:
    def __init__(self, clf, card: ModelCard, mean_amount: dict[str, float]):
        self.clf, self.card, self.mean_amount = clf, card, mean_amount

    def predict(self, store: Store) -> list[Prediction]:
        customers = {c.id: c for c in store.list(Customer)}
        out = []
        for inv in store.list(ARInvoice):
            c = customers.get(inv.customer_id)
            if c is None or inv.status not in ("issued", "overdue"):
                continue
            x = np.array([_features(c, inv.amount_cents, inv.due_date, self.mean_amount.get(c.id, inv.amount_cents))])
            proba = self.clf.predict_proba(x)[0]
            dist = {int(k): round(float(p), 4) for k, p in zip(self.clf.classes_, proba)}
            for b in BUCKETS:
                dist.setdefault(b, 0.0)
            out.append(Prediction(
                invoice_id=inv.id, customer=c.name, amount_cents=inv.amount_cents, due_date=inv.due_date,
                p_late=round(1 - dist[0], 4), p_late_3plus=dist[3], distribution=dict(sorted(dist.items())),
                assumed_distribution={b: round(v, 4) for b, v in _dist(c).items()},
                expected_days_late=round(sum(k * v for k, v in dist.items()), 3)))
        return sorted(out, key=lambda p: -p.p_late * p.amount_cents)


def train(store: Store) -> PaymentTimingModel:
    from sklearn.ensemble import HistGradientBoostingClassifier

    customers = {c.id: c for c in store.list(Customer)}
    if not customers:
        raise ValueError("no customers to learn from")
    rng = np.random.default_rng(SEED)
    real = _real_history(store, customers)
    synth = synthetic_history(list(customers.values()), rng)
    rows = synth + real
    mean_amount = {cid: float(np.mean([r[1] for r in rows if r[0].id == cid] or [1.0])) for cid in customers}
    X = np.array([_features(c, a, d, mean_amount[c.id]) for c, a, d, _ in rows])
    y = np.array([_bucket(days) for *_, days in rows])
    idx = rng.permutation(len(rows))
    cut = int(len(rows) * 0.8)
    tr, te = idx[:cut], idx[cut:]
    clf = HistGradientBoostingClassifier(max_iter=80, learning_rate=0.05, max_depth=2, min_samples_leaf=40,
                                         l2_regularization=1.0, random_state=SEED)
    clf.fit(X[tr], y[tr])
    proba = np.zeros((len(te), len(BUCKETS)))
    proba[:, list(clf.classes_)] = clf.predict_proba(X[te])
    hist = np.array([[_dist(rows[i][0])[b] for b in BUCKETS] for i in te])
    ll_model, ll_hist = _log_loss(proba, y[te]), _log_loss(hist, y[te])
    clf.fit(X, y)       # final model on everything
    card = ModelCard(
        trained_on_real=len(real), trained_on_synthetic=len(synth), holdout_rows=len(te),
        holdout_log_loss_model=round(ll_model, 4), holdout_log_loss_histogram=round(ll_hist, 4),
        improvement_pct=round(100 * (ll_hist - ll_model) / ll_hist, 1), features=FEATURES,
        algorithm="scikit-learn HistGradientBoostingClassifier (4 classes: 0, 1, 2, 3+ days late)",
        note="Trained mostly on SYNTHETIC history from a documented generating rule (see module docstring); "
             "real paid invoices are included. Advisory only: the engine keeps its documented distributions.")
    return PaymentTimingModel(clf, card, mean_amount)


_CACHE: dict[str, PaymentTimingModel] = {}
_LOCK = threading.Lock()


def get_model(store: Store) -> PaymentTimingModel:
    """Trained once per (customers, paid-invoice) state; a sync with new history retrains."""
    key = "|".join(sorted(f"{c.id}:{sorted(c.delay_distribution.items())}" for c in store.list(Customer)))
    key += "|" + ",".join(sorted(i.id for i in store.list(ARInvoice) if i.status == "paid"))
    with _LOCK:
        m = _CACHE.get(key)
        if m is None:
            m = _CACHE[key] = train(store)
        return m


def summary(store: Store) -> dict[str, Any]:
    m = get_model(store)
    return {"model_card": m.card.model_dump(), "predictions": [p.model_dump(mode="json") for p in m.predict(store)]}
