"""Ad-hoc purchases without from_account: vendor default, then primary operating account."""
import pytest
from fastapi.testclient import TestClient

from app.config import CASHGUARD_API_KEY
from app.main import create_app
from app.services.decision import resolve_from_account, ProposedPayment
from app.services.stress import _CACHE
from app.workflows.models import APBill, Vendor
from app.workflows.store import Store

AUTH = {"Authorization": f"Bearer {CASHGUARD_API_KEY}"}


@pytest.fixture
def client():
    c = TestClient(create_app(Store()))
    snap = c.post("/v1/connections/fixture/sync", headers=AUTH).json()["snapshot_id"]
    yield c
    _CACHE.pop(snap, None)


def _sim(c, **action):
    return c.post("/v1/twin/simulations", headers=AUTH,
                  json={"proposed_action": {"kind": "payment", "amount_cents": 100000, **action}})


def test_known_vendor_uses_vendor_default(client):
    store = client.app.state.store
    vendor = store.list(Vendor)[0]
    bills = [b for b in store.list(APBill) if b.vendor_id == vendor.id]
    want = max(bills, key=lambda b: (b.due_date, b.id)).source_account_id
    r = _sim(client, payee=vendor.name)
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["from_account"], d["from_account_source"]) == (want, "vendor_default")


def test_unknown_payee_uses_primary_operating(client):
    r = _sim(client, payee="Some New Supplier Inc")
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["from_account"], d["from_account_source"]) == ("operating", "primary_operating")
    run = client.get(f"/v1/runs/{d['run_id']}", headers=AUTH).json()
    assert run["result"]["proposed_action"]["from_account"] == "operating"


def test_explicit_account_wins(client):
    d = _sim(client, payee="northwind", from_account="reserve").json()
    assert (d["from_account"], d["from_account_source"]) == ("reserve", "explicit")


def test_missing_amount_still_422(client):
    r = client.post("/v1/twin/simulations", headers=AUTH,
                    json={"proposed_action": {"kind": "payment", "payee": "northwind"}})
    assert r.status_code == 422


def test_commit_works_for_defaulted_run(client):
    d = _sim(client, payee="Some New Supplier Inc", amount_cents=10000).json()
    assert d["decision"] == "allow"
    r = client.post("/v1/purchases/commit", headers={**AUTH, "Idempotency-Key": "fa1"},
                    json={"run_id": d["run_id"], "fix_id": None})
    assert r.status_code == 200, r.text
    bills = [b for b in client.app.state.store.list(APBill) if b.id.startswith("PUR-")]
    assert bills and bills[0].source_account_id == "operating"


def test_resolver_tiebreak_is_deterministic(client):
    a = resolve_from_account(client.app.state.store, ProposedPayment(amount_cents=1, payee="zzz"))
    assert a == resolve_from_account(client.app.state.store, ProposedPayment(amount_cents=1, payee="zzz"))
