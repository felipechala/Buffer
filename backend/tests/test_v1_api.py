"""Public /v1 API roundtrip in fixture mode: sync → state → simulate → approve → execute → events."""
import pytest
from fastapi.testclient import TestClient

from app.config import CASHGUARD_API_KEY
from app.main import create_app
from app.workflows.store import Store

AUTH = {"Authorization": f"Bearer {CASHGUARD_API_KEY}"}


@pytest.fixture
def client():
    app = create_app(Store())
    c = TestClient(app)
    assert c.post("/v1/connections/fixture/sync", headers=AUTH).status_code == 200
    return c


def test_requires_bearer_token(client):
    assert client.get("/v1/twin/state").status_code == 401
    bad = client.get("/v1/twin/state", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401 and bad.json()["error"]["type"] == "unauthenticated"


def test_connections_and_state(client):
    conns = client.get("/v1/connections", headers=AUTH).json()
    assert conns["mode"] == "fixture" and conns["writes_enabled"] is False
    assert {c["provider"] for c in conns["connections"]} == {"fixture", "nessie"}
    state = client.get("/v1/twin/state", headers=AUTH).json()
    assert state["company"].startswith("Acme Co") and state["horizon_days"] == 14
    assert len(state["accounts"]) == 6 and state["accounts"][0]["provider"] == "fixture"
    assert any(o["id"] == "PAYROLL-1124" and o["nondeferrable"] for o in state["obligations"])


def test_forecast_shapes(client):
    f = client.post("/v1/twin/forecasts", headers=AUTH, json={"scenario_count": 50}).json()
    assert f["horizon_days"] == 14 and len(f["accounts"]) == 6
    op = next(a for a in f["accounts"] if a["account_id"] == "operating")
    assert len(op["baseline"]) == len(op["p10"]) == len(op["p90"]) == len(op["dates"]) == 14
    assert all(lo <= hi for lo, hi in zip(op["p10"], op["p90"]))
    assert f["baseline_metrics"]["breach"] is False


def test_counterfactual_simulation(client):
    r = client.post("/v1/twin/simulations", headers=AUTH, json={
        "scenario": {"id": "judge", "receipt_delays": {"INV-4471": 3}}}).json()
    assert r["before_metrics"]["breach"] is False and "operating" in r["after"]


def test_full_decision_loop(client):
    allow = client.post("/v1/twin/simulations", headers=AUTH, json={
        "proposed_action": {"bill_id": "BILL-LEASE-11", "pay_date": "2026-11-20"}}).json()
    assert allow["decision"] == "allow"

    warn = client.post("/v1/twin/simulations", headers=AUTH, json={
        "proposed_action": {"bill_id": "BILL-NW-4471", "pay_date": "2026-11-20"}}).json()
    assert warn["decision"] == "warn" and warn["fixes"]
    assert warn["provenance"]["engine_version"] and warn["provenance"]["snapshot_id"]
    fix = next(f for f in warn["fixes"] if f["kind"] == "funding")

    # Approve: needs a finance lead; bound to the snapshot.
    denied = client.post(f"/v1/plans/{fix['id']}/approve", headers=AUTH, json={"approved_by": "u_omar"})
    assert denied.status_code == 403
    appr = client.post(f"/v1/plans/{fix['id']}/approve", headers=AUTH, json={"approved_by": "u_maya"})
    assert appr.status_code == 200 and appr.json()["subject"] == "fix"

    # Execute (simulated), idempotent.
    no_key = client.post(f"/v1/plans/{fix['id']}/execute-sandbox", headers=AUTH)
    assert no_key.status_code == 400
    headers = {**AUTH, "Idempotency-Key": "demo-exec-1"}
    ex1 = client.post(f"/v1/plans/{fix['id']}/execute-sandbox", headers=headers).json()
    ex2 = client.post(f"/v1/plans/{fix['id']}/execute-sandbox", headers=headers).json()
    assert ex1["mode"] == "simulated" and ex1 == ex2 and len(ex1["cash_event_ids"]) == 2
    assert ex1["snapshot_after"] != ex1["snapshot_before"]

    # The same payment is now safe; the old fix is stale.
    again = client.post("/v1/twin/simulations", headers=AUTH, json={
        "proposed_action": {"bill_id": "BILL-NW-4471", "pay_date": "2026-11-20"}}).json()
    assert again["decision"] == "allow"
    stale = client.post(f"/v1/plans/{fix['id']}/approve", headers=AUTH, json={"approved_by": "u_maya"})
    assert stale.status_code == 409

    actions = {e["action"] for e in client.get("/v1/events", headers=AUTH).json()}
    assert {"simulation.decide", "fix.create", "approval.fix", "fix.executed", "cashevent.post"} <= actions
    assert client.get(f"/v1/runs/{warn['run_id']}", headers=AUTH).status_code == 200


def test_block_and_adhoc(client):
    block = client.post("/v1/twin/simulations", headers=AUTH, json={
        "proposed_action": {"bill_id": "BILL-EQUIP-9", "pay_date": "2026-11-20"}}).json()
    assert block["decision"] == "block" and block["fixes"] == [] and block["infeasibility"]
    adhoc = client.post("/v1/twin/simulations", headers=AUTH, json={
        "proposed_action": {"amount_cents": 4000000, "from_account": "operating", "payee": "northwind",
                            "pay_date": "2026-11-20", "latest_date": "2026-11-27"}}).json()
    assert adhoc["decision"] == "warn" and adhoc["payee"] == "Northwind Supplies"


def test_stress_test_endpoints(client):
    started = client.post("/v1/twin/stress-tests", headers=AUTH)
    assert started.status_code == 202
    run = client.get(f"/v1/twin/stress-tests/{started.json()['run_id']}", headers=AUTH).json()
    assert run["status"] == "completed" and any(f["scenario_id"] == "acme-late-3d" for f in run["findings"])


def test_fixture_resync_without_reset_keeps_executions_and_audit(client):
    nw = {"proposed_action": {"bill_id": "BILL-NW-4471", "pay_date": "2026-11-20"}}
    warn = client.post("/v1/twin/simulations", headers=AUTH, json=nw).json()
    assert warn["decision"] == "warn"
    fix = next(f for f in warn["fixes"] if f["kind"] == "funding")
    assert client.post(f"/v1/plans/{fix['id']}/approve", headers=AUTH, json={"approved_by": "u_maya"}).status_code == 200
    ex = client.post(f"/v1/plans/{fix['id']}/execute-sandbox", headers={**AUTH, "Idempotency-Key": "k-resync"})
    assert ex.status_code == 200
    sync = client.post("/v1/connections/fixture/sync", headers=AUTH).json()
    assert sync["snapshot_id"] == ex.json()["snapshot_after"]
    assert client.post("/v1/twin/simulations", headers=AUTH, json=nw).json()["decision"] == "allow"
    actions = {e["action"] for e in client.get("/v1/events", headers=AUTH).json()}
    assert {"approval.fix", "fix.executed"} <= actions
    # explicit reset restores the original demo state
    client.post("/v1/connections/fixture/sync?reset=true", headers=AUTH)
    assert client.post("/v1/twin/simulations", headers=AUTH, json=nw).json()["decision"] == "warn"


def test_scenario_with_proposed_action_joins_stress_set(client):
    r = client.post("/v1/twin/simulations", headers=AUTH, json={
        "proposed_action": {"bill_id": "BILL-NW-4471", "pay_date": "2026-11-20"},
        "scenario": {"receipt_delays": {"INV-4471": 5}}}).json()
    assert r["provenance"]["request_scenario"]["receipt_delays"] == {"INV-4471": 5}
    assert r["provenance"]["scenarios_checked"] >= 2
    assert r["decision"] in ("warn", "block")
    assert r["provenance"]["request_scenario"]["id"] == "request_scenario"
    assert r["breaks"] and all(b["scenario"]["id"] == "request_scenario" for b in r["breaks"])
