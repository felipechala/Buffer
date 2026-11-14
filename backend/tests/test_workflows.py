import sqlite3
from datetime import date

import pytest

from app.config import NORTHSTAR_FIXTURE
from app.workflows import ap, ar
from app.workflows.forecast import build_twin_from_store
from app.workflows.models import AuditEvent, CashEvent, PaymentIntent
from app.workflows.seed import seed_demo
from app.workflows.state import WorkflowError
from app.workflows.store import Store


@pytest.fixture
def store():
    s = Store()
    seed_demo(s, NORTHSTAR_FIXTURE)
    return s


@pytest.fixture
def users(store):
    cfg = store.get_config()
    return {u.role: u for u in cfg.users}


def opening(store, account):
    return build_twin_from_store(store).account(account).observed_balance_cents


def receipt_ids(store):
    return {r.id for r in build_twin_from_store(store).receipts}


# ------------------------------------------------------------------ forecast mapping
def test_forecast_mapping_from_demo(store):
    twin = build_twin_from_store(store)
    assert receipt_ids(store) == {"INV-1001", "INV-1002", "INV-1003"}   # draft/paid excluded
    assert {o.id for o in twin.obligations} == {"BILL-17", "BILL-18", "BILL-19", "PAYROLL-1118"}
    payroll = next(o for o in twin.obligations if o.id == "PAYROLL-1118")
    assert payroll.due_day == 2 and not payroll.deferrable and payroll.priority == 0
    # Historical paid invoice is already in the snapshot: not double-counted.
    assert opening(store, "collections") == 2_000_000


def test_unpaid_invoice_never_counted_as_posted_cash(store, users):
    before = opening(store, "collections")
    inv = ar.create_invoice(store, users["ar_specialist"], ar.InvoiceCreate(
        customer_id="acme", amount_cents=500_000, issue_date=date(2026, 11, 16),
        due_date=date(2026, 11, 16), deposit_account_id="collections"))
    assert opening(store, "collections") == before and inv.id not in receipt_ids(store)
    ar.issue_invoice(store, users["ar_specialist"], inv.id)
    assert opening(store, "collections") == before and inv.id in receipt_ids(store)
    ar.mark_invoice_paid(store, users["ar_specialist"], inv.id, date(2026, 11, 16))
    assert opening(store, "collections") == before + 500_000
    assert inv.id not in receipt_ids(store)


def test_changing_invoice_date_moves_forecast(store, users):
    ar.update_invoice_dates(store, users["ar_specialist"], "INV-1001",
                            ar.InvoiceDates(expected_receipt_date=date(2026, 11, 21)))
    r = next(r for r in build_twin_from_store(store).receipts if r.id == "INV-1001")
    assert r.expected_day == 5


def test_duplicate_provider_event_rejected(store, users):
    ar.mark_invoice_paid(store, users["ar_specialist"], "INV-1002", date(2026, 11, 16), "nessie-dep-1")
    with pytest.raises(WorkflowError):
        ar.mark_invoice_paid(store, users["ar_specialist"], "INV-1001", date(2026, 11, 16), "nessie-dep-1")


# ------------------------------------------------------------------ state machines
def test_invalid_transitions_rejected(store, users):
    with pytest.raises(WorkflowError):
        ar.issue_invoice(store, users["ar_specialist"], "INV-1005")          # paid -> issued
    with pytest.raises(WorkflowError):
        ap.schedule_bill(store, users["finance_lead"], "BILL-17", date(2026, 11, 20))  # pending


def test_policy_routes_large_and_over_budget_bills(store, users):
    omar = users["ap_specialist"]
    big = ap.create_bill(store, omar, ap.BillCreate(vendor_id="vertex", amount_cents=1_500_000,
                                                    due_date=date(2026, 11, 25)))
    assert ap.submit_bill(store, omar, big.id).required_role == "finance_lead"
    small = ap.create_bill(store, omar, ap.BillCreate(vendor_id="vertex", amount_cents=50_000,
                                                      due_date=date(2026, 11, 25)))
    assert ap.submit_bill(store, omar, small.id).required_role == "ap_specialist"
    # BILL-20 ($3,200 software) pushes software over its $10k budget.
    over = ap.submit_bill(store, omar, "BILL-20")
    assert over.required_role == "finance_lead"
    assert not ap.bill_policy(store, "BILL-20").within_budget


def test_approval_gating(store, users):
    with pytest.raises(WorkflowError) as e:
        ap.decide_bill(store, users["ap_specialist"], "BILL-17", "approved")
    assert e.value.status == 403
    assert ap.decide_bill(store, users["finance_lead"], "BILL-17", "approved").status == "approved"


def test_creator_cannot_self_approve(store, users):
    maya = users["finance_lead"]
    bill = ap.create_bill(store, maya, ap.BillCreate(vendor_id="vertex", amount_cents=50_000,
                                                     due_date=date(2026, 11, 25)))
    ap.submit_bill(store, maya, bill.id)
    with pytest.raises(WorkflowError):
        ap.decide_bill(store, maya, bill.id, "approved")


def test_schedule_requires_legal_date_and_is_idempotent(store, users):
    maya = users["finance_lead"]
    ap.decide_bill(store, maya, "BILL-17", "approved")
    with pytest.raises(WorkflowError):
        ap.schedule_bill(store, maya, "BILL-17", date(2026, 11, 22))   # after due date
    first = ap.schedule_bill(store, maya, "BILL-17", date(2026, 11, 21))
    again = ap.schedule_bill(store, maya, "BILL-17", date(2026, 11, 21))   # duplicate request
    assert again.id == first.id
    live = [p for p in store.list(PaymentIntent) if p.bill_id == "BILL-17" and p.status != "canceled"]
    assert [p.id for p in live] == [first.id]
    obligation = next(o for o in build_twin_from_store(store).obligations if o.id == "BILL-17")
    assert obligation.due_day == 5


def test_simulated_payment_posts_cash_once(store, users):
    maya = users["finance_lead"]
    ap.decide_bill(store, maya, "BILL-17", "approved")
    intent = ap.schedule_bill(store, maya, "BILL-17", date(2026, 11, 16))
    before = opening(store, "operating")
    ap.complete_payment_simulated(store, maya, intent.id)
    ap.complete_payment_simulated(store, maya, intent.id)   # duplicate request: no-op
    assert opening(store, "operating") == before - 7_500_000
    assert len([e for e in store.list(CashEvent) if e.reference == "BILL-17"]) == 1
    assert "BILL-17" not in {o.id for o in build_twin_from_store(store).obligations}


# ------------------------------------------------------------------ audit
def test_every_mutation_audited_and_append_only(store, users):
    ap.decide_bill(store, users["finance_lead"], "BILL-17", "approved", memo="ok")
    trail = store.audit(entity_id="BILL-17")
    assert {"approval.approved", "bill.approved"} <= {e.action for e in trail}
    changed = next(e for e in trail if e.action == "bill.approved")
    assert changed.before["status"] == "pending" and changed.after["status"] == "approved"
    with pytest.raises(sqlite3.DatabaseError):
        store._conn.execute("UPDATE audit SET body='{}'")
    with pytest.raises(sqlite3.DatabaseError):
        store._conn.execute("DELETE FROM audit")


def test_reschedule_cancels_previous_intent(store, users):
    maya = users["finance_lead"]
    ap.decide_bill(store, maya, "BILL-17", "approved")
    first = ap.schedule_bill(store, maya, "BILL-17", date(2026, 11, 21))
    second = ap.schedule_bill(store, maya, "BILL-17", date(2026, 11, 19))
    intents = {p.id: p.status for p in store.list(PaymentIntent) if p.bill_id == "BILL-17"}
    assert intents == {first.id: "canceled", second.id: "approved"}
    assert ap.get_bill(store, "BILL-17").scheduled_date == date(2026, 11, 19)
