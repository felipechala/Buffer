"""Normalizer + sync on hand-built payloads that follow fixtures/nessie/schema_notes.md exactly."""
from datetime import date as d
from datetime import datetime, timezone

import pytest

from app.config import CASHGUARD_DEMO_FIXTURE
from app.connectors.base import RawRecord, RawSnapshot
from app.ingest.history import PaymentObservation, apply_history, build_history
from app.ingest.normalize import NormalizeError, normalize
from app.ingest.sync import apply_batch, load_overlay
from app.workflows.forecast import build_twin_from_store
from app.workflows.models import APBill, ARInvoice, CashEvent, Customer
from app.workflows.store import Store

NOW = datetime(2026, 11, 20, 13, tzinfo=timezone.utc)
OVERLAY = load_overlay(CASHGUARD_DEMO_FIXTURE)
BALANCES = {"Operating": 440000, "Payroll": 40000, "Tax": 60000, "Strategic Reserve": 900000,
            "Merchant Settlement": 20000, "International Clearing": 120000}
ACCT = {nick: f"acct-{i}" for i, nick in enumerate(BALANCES)}


def rec(kind, pid, data, acct=None):
    return RawRecord(kind, "nessie", pid, NOW, data, account_provider_id=acct)


def snapshot(deposits=(), withdrawals=(), bills=(), transfers=()):
    accounts = [rec("account", ACCT[n], {"_id": ACCT[n], "type": "Checking", "nickname": n, "rewards": 0,
                                         "balance": b, "account_number": "1234567890123456",
                                         "customer_id": "cust-1"})
                for n, b in BALANCES.items()]
    return RawSnapshot("nessie", NOW, accounts=accounts, bills=list(bills), deposits=list(deposits),
                       withdrawals=list(withdrawals), transfers=list(transfers))


def deposit(pid, status, date, amount, desc, acct="Operating"):
    return rec("deposit", pid, {"_id": pid, "medium": "balance", "transaction_date": date, "status": status,
                                "amount": amount, "description": desc}, ACCT[acct])


def bill(pid, nick, payee, due, amount, acct="Operating"):
    return rec("bill", pid, {"_id": pid, "status": "pending", "payee": payee, "nickname": nick,
                             "creation_date": "2026-11-14", "payment_date": due, "recurring_date": int(due[8:]),
                             "upcoming_payment_date": due, "payment_amount": float(amount),
                             "account_id": ACCT[acct]}, ACCT[acct])


NW = bill("b1", "BILL-NW-4471|2026-11-20|A", "Northwind Supplies", "2026-11-27", 40000)
EQUIP = bill("b2", "BILL-EQUIP-9|2026-11-20|D", "Precision Tooling Inc", "2026-11-20", 450000)
PENDING = deposit("d1", "pending", "2026-11-22", 220000, "INV-4471 acme-industrial due 2026-11-22")
PAID = [deposit(f"h{i}", "completed", paid, 1000 + i, f"INV-H{i} acme-industrial due {due}")
        for i, (paid, due) in enumerate([("2026-11-03", "2026-10-30"), ("2026-09-29", "2026-09-28"),
                                         ("2026-08-29", "2026-08-29"), ("2026-08-02", "2026-07-30")])]


def counts(store):
    return {c.__name__: len(store.list(c)) for c in (APBill, ARInvoice, CashEvent, Customer)}


def test_accounts_bills_and_pending_deposit_mapping():
    b = normalize(snapshot([PENDING, *PAID], bills=[NW, EQUIP]), OVERLAY)
    ops = next(a for a in b.config.accounts if a.id == "operating")
    assert ops.observed_balance_cents == 44_000_000 and ops.provider == "nessie" and ops.provider_id == ACCT["Operating"]
    assert ops.buffer_cents == 10_000_000                       # buffers come from the overlay, not Nessie
    nw = next(x for x in b.bills if x.id == "BILL-NW-4471")
    assert (nw.amount_cents, nw.status, nw.category, nw.vendor_id) == (4_000_000, "approved", "supplier", "northwind")
    assert nw.earliest_pay_date.isoformat() == "2026-11-20" and nw.provider == "nessie" and nw.provider_id == "b1"
    assert next(x for x in b.bills if x.id == "BILL-EQUIP-9").status == "draft"
    inv = next(i for i in b.invoices if i.id == "INV-4471")
    assert inv.status == "issued" and inv.amount_cents == 22_000_000 and inv.deposit_account_id == "operating"
    assert inv.customer_id == "acme-industrial" and inv.expected_receipt_date.isoformat() == "2026-11-22"


def test_pending_deposit_is_never_cash():
    b = normalize(snapshot([PENDING]), OVERLAY)
    assert b.cash_events == []                                  # pending -> no CashEvent
    store = Store()
    apply_batch(store, b)
    assert store.list(CashEvent) == [] and [i.id for i in store.list(ARInvoice)] == ["INV-4471"]
    twin = build_twin_from_store(store)
    assert next(a for a in twin.accounts if a.id == "operating").observed_balance_cents == 44_000_000
    assert [r.id for r in twin.receipts] == ["INV-4471"]        # a forecast receipt, not an opening balance


def test_pending_deposit_without_invoice_description_is_skipped_with_warning():
    b = normalize(snapshot([deposit("d9", "pending", "2026-11-28", 500, "mystery")]), OVERLAY)
    assert b.invoices == [] and b.cash_events == [] and any("d9" in w for w in b.warnings)


def test_completed_history_is_cash_included_in_snapshot_and_own_movements_are_not():
    own_out = rec("withdrawal", "w1", {"_id": "w1", "type": "withdrawal", "transaction_date": "2026-11-20",
                                       "status": "completed", "payer_id": "x", "medium": "balance", "amount": 100,
                                       "description": "TreasuryTwin plan_fix-1 transfer reserve->operating"},
                  ACCT["Strategic Reserve"])
    own_in = deposit("d2", "completed", "2026-11-21", 100, "TreasuryTwin plan_fix-1 transfer reserve->operating")
    b = normalize(snapshot([PAID[0], own_in], withdrawals=[own_out]), OVERLAY)
    by_ref = {e.provider_ref: e for e in b.cash_events}
    assert by_ref["h0"].kind == "receipt" and by_ref["h0"].included_in_snapshot is True
    assert by_ref["w1"].kind == "transfer_out" and by_ref["w1"].signed_cents == -10_000
    assert by_ref["w1"].included_in_snapshot is False and by_ref["d2"].kind == "transfer_in"
    store = Store()
    apply_batch(store, b)
    bal = {a.id: a.observed_balance_cents for a in build_twin_from_store(store).accounts}
    assert bal["reserve"] == 90_000_000 - 10_000                 # own movement applied once, history not double counted


def test_double_ingest_does_not_duplicate_and_keeps_snapshot():
    store = Store()
    raw = snapshot([PENDING, *PAID], bills=[NW, EQUIP])
    first = apply_batch(store, normalize(raw, OVERLAY))
    twin1 = build_twin_from_store(store)
    before = counts(store)
    audit_n = len(store.audit(limit=10_000))
    second = apply_batch(store, normalize(raw, OVERLAY))
    assert counts(store) == before
    assert second.inserted == {} and second.updated == {} and second.removed == {}
    assert len(store.audit(limit=10_000)) == audit_n                # no audit noise
    assert build_twin_from_store(store).snapshot_id == twin1.snapshot_id
    assert first.inserted["APBill"] == 2 and before["CashEvent"] == 4


def test_resync_updates_changed_and_removes_vanished_provider_objects():
    store = Store()
    apply_batch(store, normalize(snapshot([PENDING], bills=[NW, EQUIP]), OVERLAY))
    nw2 = bill("b1", "BILL-NW-4471|2026-11-20|A", "Northwind Supplies", "2026-11-27", 41000)
    res = apply_batch(store, normalize(snapshot([PENDING], bills=[nw2]), OVERLAY))
    assert store.get(APBill, "BILL-NW-4471").amount_cents == 4_100_000
    assert store.get(APBill, "BILL-EQUIP-9") is None and res.removed["APBill"] == 1


def test_first_sync_replaces_fixture_seeded_state():
    from app.workflows.seed import seed_demo
    store = Store()
    seed_demo(store, CASHGUARD_DEMO_FIXTURE)
    apply_batch(store, normalize(snapshot([PENDING], bills=[NW]), OVERLAY))
    assert [b.id for b in store.list(APBill)] == ["BILL-NW-4471"]
    assert store.get_config().accounts[0].provider == "nessie"


def test_unmapped_account_nickname_is_warned_and_missing_internal_account_fails():
    snap = snapshot()
    snap.accounts.append(rec("account", "x", {"_id": "x", "nickname": "Mystery", "balance": 1}))
    assert any("Mystery" in w for w in normalize(snap, OVERLAY).warnings)
    snap.accounts = snap.accounts[1:]
    with pytest.raises(NormalizeError) as e:
        normalize(snap, OVERLAY)
    assert e.value.tag == "missing_accounts"


def test_bad_schema_raises_tagged_error_with_redacted_sample():
    bad = rec("account", "z", {"_id": "z", "nickname": "Operating", "account_number": "9999", "customer_id": "c"})
    snap = snapshot()
    snap.accounts[0] = bad
    with pytest.raises(NormalizeError) as e:
        normalize(snap, OVERLAY)
    assert e.value.tag == "account_schema" and e.value.sample["customer_id"] == "<redacted>"


def test_history_on_time_share_and_delay_distribution():
    obs = [PaymentObservation("a", "1", d(2026, 1, 1), d(2026, 1, 1)),
           PaymentObservation("a", "2", d(2026, 2, 1), d(2026, 2, 5)),
           PaymentObservation("a", "3", d(2026, 3, 1), d(2026, 3, 1)),
           PaymentObservation("a", "4", d(2026, 4, 1), d(2026, 3, 30))]
    h = build_history(obs)["a"]
    assert h.samples == 4 and h.on_time_share == 0.75 and h.delay_distribution == {0: 0.75, 4: 0.25}
    cust = [Customer(id="a", name="A"), Customer(id="b", name="B")]
    out = apply_history(cust, {"a": h}, min_samples=3)
    assert out[0].delay_distribution == {0: 0.75, 4: 0.25} and out[1].delay_distribution == {0: 1.0}
    assert apply_history(cust, {"a": h}, min_samples=5)[0].delay_distribution == {0: 1.0}


def test_normalize_applies_history_to_customers():
    b = normalize(snapshot([*PAID]), OVERLAY)
    acme = next(c for c in b.customers if c.id == "acme-industrial")
    assert acme.delay_distribution == {0: 0.25, 1: 0.25, 3: 0.25, 4: 0.25}
    assert next(c for c in b.customers if c.id == "globex").delay_distribution == {0: 0.75, 1: 0.2, 2: 0.05}
