"""Nessie connector over httpx.MockTransport + the fixture connector. Offline."""
import json

import httpx
import pytest

from app.connectors.base import ConnectorAuthError, ConnectorUnavailable, fetch_snapshot
from app.connectors.fixture import FixtureConnector
from app.connectors.nessie import NessieClient, NessieConnector

CUST = "cust-1"
KEY = "secret-key-123"


def make(handler, **kw):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = NessieClient("https://nessie.test", KEY, client=http, sleep=lambda s: None, **kw)
    return NessieConnector(client, customer_id=CUST)


def test_health_ok_requires_customer_to_resolve():
    c = make(lambda r: httpx.Response(200, json={"_id": CUST, "first_name": "Acme"}))
    h = c.health()
    assert h.ok and h.status == "healthy"


def test_health_fails_when_customer_404s_even_if_lists_return_200():
    def handler(req):
        if req.url.path == f"/customers/{CUST}":
            return httpx.Response(404, json="Customer not found")
        return httpx.Response(200, json=[])          # bad keys / unknown ids still answer 200 []
    h = make(handler).health()
    assert not h.ok and h.status == "unavailable" and "did not resolve" in h.detail


def test_health_reports_auth_and_not_configured():
    assert make(lambda r: httpx.Response(403, text="no")).health().status == "auth_error"
    c = NessieConnector(NessieClient("https://x", ""), customer_id=CUST)
    assert c.health().status == "not_configured"


def test_5xx_retried_then_connector_unavailable_without_leaking_key():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(503, text="down")
    c = make(handler, max_retries=2)
    with pytest.raises(ConnectorUnavailable) as e:
        c.fetch_accounts()
    assert len(calls) == 3 and KEY not in str(e.value)


def test_retry_then_success_on_429():
    seq = iter([httpx.Response(429), httpx.Response(200, json=[{"_id": "a1", "nickname": "Operating", "balance": 5}])])
    recs = make(lambda r: next(seq)).fetch_accounts()
    assert [r.provider_id for r in recs] == ["a1"]


def test_timeout_maps_to_unavailable_and_hides_url():
    def handler(req):
        raise httpx.ConnectTimeout("boom", request=req)
    with pytest.raises(ConnectorUnavailable) as e:
        make(handler, max_retries=1).fetch_accounts()
    assert KEY not in str(e.value) and "ConnectTimeout" in str(e.value)


def test_401_maps_to_auth_error_without_retry():
    n = []
    c = make(lambda r: (n.append(1), httpx.Response(401))[1])
    with pytest.raises(ConnectorAuthError):
        c.fetch_accounts()
    assert len(n) == 1


def test_fetch_is_customer_scoped_and_handles_transfer_id_and_404_empty():
    paths = []

    def handler(req):
        paths.append(req.url.path)
        assert req.url.params["key"] == KEY
        p = req.url.path
        if p == f"/customers/{CUST}/accounts":
            return httpx.Response(200, json=[{"_id": "a1", "nickname": "Operating", "balance": 10}])
        if p == "/accounts/a1/deposits":
            return httpx.Response(200, json=[{"_id": "d1", "status": "pending", "amount": 5, "description": "x",
                                              "transaction_date": "2026-11-22", "medium": "balance"}])
        if p == "/accounts/a1/transfers":
            return httpx.Response(200, json=[{"id": "t1", "status": "pending", "amount": 1, "description": "y",
                                              "transaction_date": "2026-11-22"}])
        if p == "/accounts/a1/withdrawals":
            return httpx.Response(404, json="No withdrawals found for this account")
        if p == "/accounts/a1/bills":
            return httpx.Response(200, json=[{"_id": "b1", "payee": "Northwind Supplies", "nickname": "N|2026-11-20|A"}])
        return httpx.Response(500)
    snap = fetch_snapshot(make(handler))
    assert [d.provider_id for d in snap.deposits] == ["d1"] and snap.deposits[0].account_provider_id == "a1"
    assert [t.provider_id for t in snap.transfers] == ["t1"] and snap.withdrawals == []
    assert snap.counterparties == ["Northwind Supplies"]
    assert "/accounts" not in paths and "/merchants" not in paths     # never sandbox-wide


def test_malformed_body_is_unavailable():
    with pytest.raises(ConnectorUnavailable):
        make(lambda r: httpx.Response(200, json={"not": "a list"})).fetch_accounts()


def test_http_base_url_is_upgraded_for_nessie_host():
    assert NessieClient("http://api.nessieisreal.com", "k").base_url == "https://api.nessieisreal.com"


def test_fixture_connector_reads_nessie_shaped_files(tmp_path):
    (tmp_path / "accounts.json").write_text(json.dumps([{"_id": "a1", "nickname": "Operating", "balance": 1}]))
    (tmp_path / "deposits.json").write_text(json.dumps({"a1": [{"_id": "d1", "status": "pending", "amount": 3}],
                                                        "zzz": [{"_id": "dX"}]}))
    (tmp_path / "transfers.json").write_text(json.dumps([{"id": "t1", "account_id": "a1"}]))
    snap = fetch_snapshot(FixtureConnector(tmp_path))
    assert [a.provider_id for a in snap.accounts] == ["a1"]
    assert [d.provider_id for d in snap.deposits] == ["d1"] and [t.provider_id for t in snap.transfers] == ["t1"]
    assert FixtureConnector(tmp_path / "nope").health().ok is False


def test_nessie_sync_endpoint_503_keeps_last_snapshot():
    from fastapi.testclient import TestClient

    from app.config import CASHGUARD_API_KEY
    from app.main import create_app
    from app.workflows.store import Store

    auth = {"Authorization": f"Bearer {CASHGUARD_API_KEY}"}
    app = create_app(Store())
    client = TestClient(app)
    snap = client.post("/v1/connections/fixture/sync", headers=auth).json()["snapshot_id"]
    app.state.nessie_connector = make(lambda r: httpx.Response(503, text="down"), max_retries=1)
    r = client.post("/v1/connections/nessie/sync", headers=auth)
    assert r.status_code == 503 and r.json()["mode"] == "fixture" and r.json()["snapshot_id"] == snap
    assert KEY not in r.text
    conns = client.get("/v1/connections", headers=auth).json()
    nessie = next(c for c in conns["connections"] if c["provider"] == "nessie")
    assert conns["mode"] == "fixture" and nessie["status"] == "unavailable"
    assert client.get("/v1/twin/state", headers=auth).json()["snapshot_id"] == snap
