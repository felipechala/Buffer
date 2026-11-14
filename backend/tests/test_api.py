from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_sync_scenario_and_optimize_roundtrip():
    sync = client.post("/v1/twin/sync", json={"fixture": "golden"}).json()
    sid = sync["snapshot_id"]
    assert sync["counts"]["accounts"] == 2

    sim = client.post("/v1/scenarios/run", json={"snapshot_id": sid}).json()
    assert sim["metrics"]["uncovered_obligations"] == ["payroll-1"]

    plan = client.post("/v1/plans/optimize", json={"snapshot_id": sid}).json()
    assert plan["status"] == "feasible"
    assert plan["modeled_cost_cents"] == 104_000
    assert client.get(f"/v1/plans/{plan['id']}").json()["id"] == plan["id"]

    # Replay the plan through the scenario endpoint: no breach.
    replay = client.post("/v1/scenarios/run",
                         json={"snapshot_id": sid, "actions": plan["actions"]}).json()
    assert replay["metrics"]["breach"] is False


def test_overrides_create_new_snapshot_and_replan():
    sid = client.post("/v1/twin/sync", json={"fixture": "golden"}).json()["snapshot_id"]
    new = client.post(f"/v1/twin/{sid}/overrides",
                      json={"route_settlement_days": {"reserve->operating": 2}}).json()
    assert new["snapshot_id"] != sid
    plan = client.post("/v1/plans/optimize", json={"snapshot_id": new["snapshot_id"]}).json()
    assert plan["status"] == "infeasible"


def test_rejects_path_traversal_and_unknown_snapshot():
    assert client.post("/v1/twin/sync", json={"fixture": "../secrets"}).status_code == 400
    assert client.get("/v1/twin/snap_nope").status_code == 404
