"""End-to-end v2 north-star journey through the HTTP API (in-process)."""
import pytest
from fastapi.testclient import TestClient

from app.config import NORTHSTAR_FIXTURE
from app.main import create_app
from app.workflows.store import Store

MAYA = {"X-User-Id": "u_maya"}
OMAR = {"X-User-Id": "u_omar"}


@pytest.fixture
def client():
    return TestClient(create_app(Store(), NORTHSTAR_FIXTURE))


def test_bill_journey_check_impact_approve_schedule_pay(client):
    accounts = client.get("/api/accounts").json()
    assert {a["id"] for a in accounts} == {"operating", "reserve", "collections"}

    impact = client.post("/api/bills/BILL-17/impact", json={"scenario_count": 100}).json()
    assert impact["status"] in ("feasible", "requires_review")
    assert [o["pay_day"] for o in impact["options"]] == [0, 1, 5]
    assert impact["recommended_pay_day"] is not None
    assert client.get(f"/api/runs/{impact['run_id']}").status_code == 200
    rec = next(o for o in impact["options"] if o["pay_day"] == impact["recommended_pay_day"])

    # AP specialist cannot approve a >$10k bill; finance lead can.
    assert client.post("/api/bills/BILL-17/approve", json={}, headers=OMAR).status_code == 403
    assert client.post("/api/bills/BILL-17/approve", json={"memo": "ok"}, headers=MAYA).json()["status"] == "approved"

    intent = client.post("/api/bills/BILL-17/schedule",
                         json={"pay_date": rec["pay_date"]}, headers=MAYA).json()
    assert intent["status"] == "approved"

    # Fund the schedule: robust plan, approved by the finance lead.
    plan = client.post("/api/plans", json={}).json()
    assert plan["status"] == "feasible" and plan["actionable"]
    appr = client.post(f"/api/plans/{plan['id']}/approve", json={}, headers=MAYA)
    assert appr.status_code == 200

    paid = client.post(f"/api/payments/{intent['id']}/complete-simulated", headers=MAYA).json()
    assert paid["status"] == "confirmed"
    detail = client.get("/api/bills/BILL-17").json()
    assert detail["bill"]["status"] == "paid" and len(detail["approvals"]) == 1


def test_mutations_require_user_and_errors_use_envelope(client):
    r = client.post("/api/bills/BILL-17/approve", json={})
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated"
    r = client.get("/api/bills/NOPE")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_infeasible_plan_cannot_be_approved(client):
    plan = client.post("/api/plans", json={"scenario": {
        "id": "route-outage", "disabled_routes": ["reserve-draw"]}}).json()
    assert plan["status"] == "infeasible" and not plan["actionable"]
    r = client.post(f"/api/plans/{plan['id']}/approve", json={}, headers=MAYA)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_actionable"


def test_stale_plan_rejected_after_state_change(client):
    plan = client.post("/api/plans", json={}).json()
    client.patch("/api/invoices/INV-1001/dates", json={"expected_receipt_date": "2026-11-20"},
                 headers={"X-User-Id": "u_lee"})
    r = client.post(f"/api/plans/{plan['id']}/approve", json={}, headers=MAYA)
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_plan"


def test_judge_changes_input_engine_reruns(client):
    base = client.post("/api/scenarios/run", json={}).json()
    moved = client.post("/api/scenarios/run", json={"bill_pay_days": {"BILL-17": 0}}).json()
    op = lambda res: [r["closing_cents"] for r in res["ledger"] if r["account_id"] == "operating"]
    assert op(base)[0] != op(moved)[0]
