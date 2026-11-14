"""Derive the engine's TwinState from operational state.

Mapping rules (each is an explicit, testable assumption):
  * Opening balance = observed snapshot balance + posted CashEvents dated on/before
    day 0 that are not already reflected in the snapshot.
  * Posted CashEvents dated after day 0 are certain flows on their effective day.
  * Issued/overdue invoices -> ExpectedReceipt on expected_receipt_date with the
    customer's delay distribution. Draft, paid and void invoices are NOT forecast cash.
  * Pending/approved/scheduled/failed bills -> Obligation on scheduled_date if
    scheduled, otherwise on due_date (latest legal day). Draft/rejected/paid excluded.
  * `bill_pay_days` overrides a bill's pay day for counterfactual "what if" analysis,
    and also lets a draft bill be included in that analysis.
  * Dates before day 0 are clamped to day 0 (overdue items are due now).
"""
from __future__ import annotations

from datetime import date

from ..engine.models import Account, ExpectedReceipt, Obligation, TwinState
from ..engine.twin import build_twin
from .models import APBill, ARInvoice, CashEvent, CompanyConfig, Customer
from .store import Store

FORECAST_BILL_STATUSES = {"pending", "approved", "scheduled", "failed"}
FORECAST_INVOICE_STATUSES = {"issued", "overdue"}


def day_index(cfg: CompanyConfig, d: date) -> int:
    return (d - cfg.as_of).days


def build_twin_from_store(store: Store, *, bill_pay_days: dict[str, int] | None = None,
                          config: CompanyConfig | None = None,
                          extra_bills: list[APBill] | None = None) -> TwinState:
    """`extra_bills` are ephemeral (not stored) bills, e.g. an ad-hoc payment being checked.
    They are only included when `bill_pay_days` names them."""
    cfg = config or store.get_config()
    bill_pay_days = bill_pay_days or {}
    events = store.list(CashEvent)

    opening = {a.id: a.observed_balance_cents for a in cfg.accounts}
    obligations: list[Obligation] = []
    receipts: list[ExpectedReceipt] = []
    for e in events:
        if e.included_in_snapshot:
            continue
        day = day_index(cfg, e.effective_date)
        if day <= 0:
            opening[e.account_id] += e.signed_cents
        elif e.signed_cents > 0:
            receipts.append(ExpectedReceipt(id=f"posted:{e.id}", account_id=e.account_id,
                                            amount_cents=e.signed_cents, expected_day=day,
                                            counterparty=f"posted {e.kind}", source="synthetic"))
        elif e.signed_cents < 0:
            obligations.append(Obligation(id=f"posted:{e.id}", account_id=e.account_id,
                                          amount_cents=-e.signed_cents, due_day=day, priority=0,
                                          counterparty=f"posted {e.kind}", category=e.kind))

    accounts = [Account(id=a.id, name=a.name, role=a.role, observed_balance_cents=opening[a.id],
                        minimum_buffer_cents=a.buffer_cents,
                        source="nessie" if a.provider == "nessie" else "fixture",
                        nessie_id=a.provider_id)
                for a in cfg.accounts]

    customers = {c.id: c for c in store.list(Customer)}
    for inv in store.list(ARInvoice):
        if inv.status not in FORECAST_INVOICE_STATUSES:
            continue
        cust = customers.get(inv.customer_id)
        receipts.append(ExpectedReceipt(
            id=inv.id, account_id=inv.deposit_account_id, amount_cents=inv.amount_cents,
            expected_day=max(0, day_index(cfg, inv.expected_receipt_date)),
            delay_distribution=cust.delay_distribution if cust else {0: 1.0},
            correlation_key=(cust.correlation_key or cust.id) if cust else None,
            counterparty=cust.name if cust else inv.customer_id))

    for bill in [*store.list(APBill), *(extra_bills or [])]:
        override = bill_pay_days.get(bill.id)
        if override is None and bill.status not in FORECAST_BILL_STATUSES:
            continue
        if bill.status in ("paid", "rejected"):
            continue
        if override is not None:
            day = override
        elif bill.status == "scheduled" and bill.scheduled_date:
            day = day_index(cfg, bill.scheduled_date)
        else:
            day = day_index(cfg, bill.due_date)
        obligations.append(Obligation(
            id=bill.id, account_id=bill.source_account_id, amount_cents=bill.amount_cents,
            due_day=max(0, day), priority=0 if bill.nondeferrable else 1,
            deferrable=not bill.nondeferrable, counterparty=bill.vendor_id,
            category=bill.category))

    provenance = {
        "company": cfg.name,
        "balances": ",".join(sorted({a.provider for a in cfg.accounts})),
        "balances_as_of": max(a.as_of for a in cfg.accounts).isoformat(),
        "receipts": "app invoices (forecast) + posted cash events",
        "obligations": "app bills + posted cash events",
        "routes_and_credit": "synthetic configuration",
    }
    return build_twin(
        company_name=cfg.name, as_of=cfg.as_of, horizon_days=cfg.horizon_days,
        accounts=accounts, obligations=obligations, receipts=receipts,
        routes=cfg.routes, credit_lines=cfg.credit_lines,
        provenance=provenance, assumptions=cfg.assumptions)
