"""Payables workflows (Ramp-inspired): vendors, bills, approvals, payment intents."""
from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel, Field

from .models import APBill, Approval, CashEvent, PaymentIntent, User, Vendor, utcnow
from .policy import PolicyResult, can_approve, evaluate_bill
from .state import BILL_TRANSITIONS, PAYMENT_TRANSITIONS, WorkflowError, check_transition
from .store import Store


class VendorCreate(BaseModel):
    name: str = Field(min_length=1)
    category: str = Field(min_length=1)
    payment_terms_days: int = Field(default=30, ge=0, le=365)


class BillCreate(BaseModel):
    vendor_id: str
    amount_cents: int = Field(gt=0)
    due_date: date
    earliest_pay_date: date | None = None   # defaults to today (as_of)
    category: str | None = None             # defaults to vendor category
    source_account_id: str = "operating"
    memo: str = ""


def create_vendor(store: Store, user: User, req: VendorCreate) -> Vendor:
    vendor = Vendor(id=store.next_id("vend", Vendor), **req.model_dump())
    store.save(vendor, actor=user.id, action="vendor.create")
    return vendor


def get_bill(store: Store, bill_id: str) -> APBill:
    bill = store.get(APBill, bill_id)
    if bill is None:
        raise WorkflowError("not_found", f"bill {bill_id} not found", 404)
    return bill


def create_bill(store: Store, user: User, req: BillCreate) -> APBill:
    vendor = store.get(Vendor, req.vendor_id)
    if vendor is None:
        raise WorkflowError("unknown_vendor", f"vendor {req.vendor_id} not found", 400)
    cfg = store.get_config()
    if cfg.account(req.source_account_id) is None:
        raise WorkflowError("unknown_account", f"account {req.source_account_id} not found", 400)
    category = req.category or vendor.category
    try:
        bill = APBill(
            id=store.next_id("BILL", APBill), vendor_id=vendor.id, amount_cents=req.amount_cents,
            category=category, due_date=req.due_date,
            earliest_pay_date=req.earliest_pay_date or min(cfg.as_of, req.due_date),
            source_account_id=req.source_account_id,
            nondeferrable=category in cfg.policy.nondeferrable_categories,
            memo=req.memo, created_by=user.id)
    except ValueError as exc:
        raise WorkflowError("invalid_bill", str(exc), 400)
    store.save(bill, actor=user.id, action="bill.create")
    return bill


def bill_policy(store: Store, bill_id: str) -> PolicyResult:
    return evaluate_bill(store.get_config(), store, get_bill(store, bill_id))


def submit_bill(store: Store, user: User, bill_id: str) -> APBill:
    bill = get_bill(store, bill_id)
    check_transition(BILL_TRANSITIONS, "bill", bill.status, "pending")
    pol = evaluate_bill(store.get_config(), store, bill)
    new = bill.model_copy(update={"status": "pending", "required_role": pol.required_role,
                                  "nondeferrable": pol.nondeferrable})
    store.save(new, actor=user.id, action="bill.submit", before=bill)
    return new


def decide_bill(store: Store, user: User, bill_id: str, decision: str, memo: str = "") -> APBill:
    bill = get_bill(store, bill_id)
    target = "approved" if decision == "approved" else "rejected"
    check_transition(BILL_TRANSITIONS, "bill", bill.status, target)
    if not can_approve(user, bill.required_role):
        raise WorkflowError("forbidden", f"{user.role} cannot decide a bill requiring "
                            f"{bill.required_role}", 403)
    if user.id == bill.created_by:
        raise WorkflowError("forbidden", "the bill creator cannot approve their own bill", 403)
    approval = Approval(id=f"appr-{uuid.uuid4().hex[:10]}", subject="bill", subject_id=bill.id,
                        approver_id=user.id, role=user.role, decision=target,  # type: ignore[arg-type]
                        timestamp=utcnow(), memo=memo)
    new = bill.model_copy(update={"status": target})
    store.save(approval, actor=user.id, action=f"approval.{target}", correlation_id=bill.id)
    store.save(new, actor=user.id, action=f"bill.{target}", before=bill)
    return new


def schedule_bill(store: Store, user: User, bill_id: str, pay_date: date,
                  plan_id: str | None = None) -> PaymentIntent:
    """Approved bill -> scheduled payment intent on a legal date. Does not move money."""
    bill = get_bill(store, bill_id)
    key = f"{bill.id}:{pay_date.isoformat()}:{bill.amount_cents}"
    existing = next((p for p in store.list(PaymentIntent)
                     if p.idempotency_key == key and p.status != "canceled"), None)
    if existing:
        return existing  # idempotent: same request, no duplicate intent or state change
    if bill.status == "scheduled":  # reschedule: scheduled -> approved -> scheduled
        check_transition(BILL_TRANSITIONS, "bill", bill.status, "approved")
        bill = bill.model_copy(update={"status": "approved", "scheduled_date": None})
    check_transition(BILL_TRANSITIONS, "bill", bill.status, "scheduled")
    if not bill.earliest_pay_date <= pay_date <= bill.due_date:
        raise WorkflowError("illegal_date", f"pay date must be between {bill.earliest_pay_date} "
                            f"and {bill.due_date}", 400)
    if pay_date < store.get_config().as_of:
        raise WorkflowError("illegal_date", "pay date is in the past", 400)
    for p in store.list(PaymentIntent):  # cancel any earlier live schedule for this bill
        if p.bill_id == bill.id and p.status in ("proposed", "approved"):
            store.save(p.model_copy(update={"status": "canceled"}), actor=user.id,
                       action="payment.cancel", before=p, correlation_id=bill.id)
    intent = PaymentIntent(id=f"pay-{uuid.uuid4().hex[:10]}", bill_id=bill.id,
                           source_account_id=bill.source_account_id, amount_cents=bill.amount_cents,
                           scheduled_date=pay_date, status="approved", idempotency_key=key,
                           plan_id=plan_id)
    store.save(intent, actor=user.id, action="payment.schedule", correlation_id=bill.id)
    store.save(bill.model_copy(update={"status": "scheduled", "scheduled_date": pay_date}),
               actor=user.id, action="bill.scheduled", before=bill)
    return intent


def complete_payment_simulated(store: Store, user: User, payment_id: str) -> PaymentIntent:
    """Clearly-labelled local execution: submitted -> confirmed, posts a CashEvent."""
    intent = store.get(PaymentIntent, payment_id)
    if intent is None:
        raise WorkflowError("not_found", f"payment {payment_id} not found", 404)
    if intent.status == "confirmed":
        return intent  # idempotent
    check_transition(PAYMENT_TRANSITIONS, "payment", intent.status, "submitted")
    submitted = intent.model_copy(update={"status": "submitted"})
    store.save(submitted, actor=user.id, action="payment.submit", before=intent)
    confirmed = submitted.model_copy(update={"status": "confirmed",
                                             "provider_ref": f"sim-{intent.id}"})
    store.save(confirmed, actor=user.id, action="payment.confirm_simulated", before=submitted)
    event = CashEvent(id=f"cash-{intent.id}", account_id=intent.source_account_id,
                      signed_cents=-intent.amount_cents, effective_date=intent.scheduled_date,
                      kind="bill_payment", source="app_simulated", reference=intent.bill_id,
                      provider_ref=confirmed.provider_ref)
    store.save(event, actor=user.id, action="cash.post", correlation_id=intent.bill_id)
    bill = get_bill(store, intent.bill_id)
    check_transition(BILL_TRANSITIONS, "bill", bill.status, "paid")
    store.save(bill.model_copy(update={"status": "paid"}), actor=user.id, action="bill.paid",
               before=bill)
    return confirmed
