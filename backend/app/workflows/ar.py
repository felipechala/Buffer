"""Receivables workflows (Stripe-inspired): customers and invoices."""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from .models import ARInvoice, CashEvent, Customer, User
from .state import INVOICE_TRANSITIONS, WorkflowError, check_transition
from .store import Store


class CustomerCreate(BaseModel):
    name: str = Field(min_length=1)
    payment_terms_days: int = Field(default=30, ge=0, le=365)
    delay_distribution: dict[int, float] = {0: 1.0}


class InvoiceCreate(BaseModel):
    customer_id: str
    amount_cents: int = Field(gt=0)
    issue_date: date
    due_date: date
    expected_receipt_date: date | None = None   # defaults to due date
    deposit_account_id: str
    memo: str = ""


class InvoiceDates(BaseModel):
    due_date: date | None = None
    expected_receipt_date: date | None = None


def create_customer(store: Store, user: User, req: CustomerCreate) -> Customer:
    if abs(sum(req.delay_distribution.values()) - 1) > 1e-6 or any(k < 0 for k in req.delay_distribution):
        raise WorkflowError("invalid_distribution", "delay probabilities must be >= 0-day keys summing to 1", 400)
    cust = Customer(id=store.next_id("cust", Customer), **req.model_dump())
    store.save(cust, actor=user.id, action="customer.create")
    return cust


def get_invoice(store: Store, invoice_id: str) -> ARInvoice:
    inv = store.get(ARInvoice, invoice_id)
    if inv is None:
        raise WorkflowError("not_found", f"invoice {invoice_id} not found", 404)
    return inv


def create_invoice(store: Store, user: User, req: InvoiceCreate) -> ARInvoice:
    if store.get(Customer, req.customer_id) is None:
        raise WorkflowError("unknown_customer", f"customer {req.customer_id} not found", 400)
    if store.get_config().account(req.deposit_account_id) is None:
        raise WorkflowError("unknown_account", f"account {req.deposit_account_id} not found", 400)
    try:
        inv = ARInvoice(id=store.next_id("INV", ARInvoice), created_by=user.id,
                        expected_receipt_date=req.expected_receipt_date or req.due_date,
                        **req.model_dump(exclude={"expected_receipt_date"}))
    except ValueError as exc:
        raise WorkflowError("invalid_invoice", str(exc), 400)
    store.save(inv, actor=user.id, action="invoice.create")
    return inv


def update_invoice_dates(store: Store, user: User, invoice_id: str, req: InvoiceDates) -> ARInvoice:
    inv = get_invoice(store, invoice_id)
    if inv.status in ("paid", "void"):
        raise WorkflowError("immutable", f"invoice is {inv.status}; dates cannot change")
    data = inv.model_dump()
    data.update({k: v for k, v in req.model_dump().items() if v is not None})
    try:
        new = ARInvoice(**data)
    except ValueError as exc:
        raise WorkflowError("invalid_invoice", str(exc), 400)
    store.save(new, actor=user.id, action="invoice.update_dates", before=inv)
    return new


def _transition(store: Store, user: User, inv: ARInvoice, target: str, **changes) -> ARInvoice:
    check_transition(INVOICE_TRANSITIONS, "invoice", inv.status, target)
    new = inv.model_copy(update={"status": target, **changes})
    store.save(new, actor=user.id, action=f"invoice.{target}", before=inv)
    return new


def issue_invoice(store: Store, user: User, invoice_id: str) -> ARInvoice:
    return _transition(store, user, get_invoice(store, invoice_id), "issued")


def void_invoice(store: Store, user: User, invoice_id: str) -> ARInvoice:
    return _transition(store, user, get_invoice(store, invoice_id), "void")


def mark_invoice_paid(store: Store, user: User, invoice_id: str, paid_date: date,
                      provider_ref: str | None = None) -> ARInvoice:
    """Link a posted (simulated or Nessie) deposit. Only now does the cash count as observed."""
    inv = get_invoice(store, invoice_id)
    if provider_ref and any(e.provider_ref == provider_ref for e in store.list(CashEvent)):
        raise WorkflowError("duplicate_event", f"provider event {provider_ref} already recorded")
    paid = _transition(store, user, inv, "paid", paid_date=paid_date)
    event = CashEvent(id=f"cash-{inv.id}", account_id=inv.deposit_account_id,
                      signed_cents=inv.amount_cents, effective_date=paid_date, kind="receipt",
                      source="nessie" if provider_ref else "app_simulated",
                      reference=inv.id, provider_ref=provider_ref)
    store.save(event, actor=user.id, action="cash.post", correlation_id=inv.id)
    return paid
