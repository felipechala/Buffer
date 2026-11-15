"""Demo 2 (purchase checks): the four-step script through the public API, plus commit edge cases."""
import time

import pytest
from fastapi.testclient import TestClient

from app.config import CASHGUARD_API_KEY
from app.main import create_app
from app.services.stress import _CACHE
from app.workflows.models import APBill
from app.workflows.store import Store

AUTH = {"Authorization": f"Bearer {CASHGUARD_API_KEY}"}


def _body(p, amount=None):
    return {"proposed_action": {"kind": "payment", "amount_cents": amount or p["amount_cents"],
                                "from_account": p["from_account"], "payee": p["payee"], "pay_date": p["pay_date"],
                                "channel": p["channel"], "description": p["description"],
                                "requested_by": p["requested_by"]}}


def _check(c, p, amount=None):
    r = c.post("/v1/twin/simulations", headers=AUTH, json=_body(p, amount))
    assert r.status_code == 200, r.text
    return r.json()


def _commit(c, run_id, fix_id=None, key="k1", **extra):
    return c.post("/v1/purchases/commit", headers={**AUTH, "Idempotency-Key": key},
                  json={"run_id": run_id, "fix_id": fix_id, **extra})


def _purchases(c):
    return [b for b in c.app.state.store.list(APBill) if b.id.startswith("PUR-")]


@pytest.fixture
def plain():
    """Default stress set (auto-stress off, fast)."""
    c = TestClient(create_app(Store()))
    snap = c.post("/v1/connections/fixture/sync", headers=AUTH).json()["snapshot_id"]
    yield c
    _CACHE.pop(snap, None)


@pytest.fixture
def demo(plain):
    return {p["id"]: p for p in plain.get("/v1/purchases/demo", headers=AUTH).json()["requests"]}


def test_demo_requests_in_order(plain):
    assert plain.get("/v1/purchases/demo").status_code == 401
    reqs = plain.get("/v1/purchases/demo", headers=AUTH).json()["requests"]
    assert [(r["step"], r["id"], r["channel"], r["payee"]) for r in reqs] == [
        (1, "PR-LAPTOPS", "card", "dell"), (2, "PR-NW-RUSH", "chat", "northwind"),
        (3, "PR-CNC", "checkout", "haas"), (4, "PR-TRADESHOW", "card", "expo")]
    assert reqs[0]["amount_cents"] == 500000 and reqs[2]["amount_cents"] == 45000000
    assert all(r["from_account"] == "operating" and r["requested_by"] and r["description"] and r["payee_name"]
               and r["pay_date"] for r in reqs)


def test_channel_metadata_echoed_in_provenance_and_audit(plain, demo):
    d = _check(plain, demo["PR-LAPTOPS"])
    assert d["decision"] == "allow" and d["payee"] == "Dell Technologies"
    prov = d["provenance"]
    assert (prov["channel"], prov["description"], prov["requested_by"]) == (
        "card", "12 laptops for new hires", "Sam (IT)")
    run = plain.get(f"/v1/runs/{d['run_id']}", headers=AUTH).json()
    assert run["result"]["proposed_action"]["channel"] == "card"
    ev = [e for e in plain.get("/v1/events", headers=AUTH, params={"entity_id": d["run_id"]}).json()
          if e["action"] == "simulation.decide"]
    assert ev and "card" in str(ev[0]["after"])


def test_commit_validation_and_idempotency(plain, demo):
    assert _commit(plain, "run_nope").status_code == 404
    d = _check(plain, demo["PR-LAPTOPS"])
    no_key = plain.post("/v1/purchases/commit", headers=AUTH, json={"run_id": d["run_id"]})
    assert no_key.status_code == 400
    first = _commit(plain, d["run_id"], key="idem-1")
    assert first.status_code == 200
    out = first.json()
    assert out["purchase_id"].startswith("PUR-") and out["bill_id"] == out["purchase_id"]
    assert out["scheduled_date"] == "2026-11-20" and out["executed_fix"] is None and out["decision"] == "allow"
    (b,) = _purchases(plain)
    assert (b.status, b.memo, b.created_by, b.vendor_id) == ("scheduled", "12 laptops for new hires",
                                                             "Sam (IT)", "dell")
    # replay: same response, no second write
    again = _commit(plain, d["run_id"], key="idem-1")
    assert again.status_code == 200 and again.json() == out and len(_purchases(plain)) == 1
    # a new key on the same run: the snapshot moved, so the check is stale
    stale = _commit(plain, d["run_id"], key="idem-2")
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_snapshot"
    actions = {e["action"] for e in plain.get("/v1/events", headers=AUTH, params={"entity_id": d["run_id"]}).json()}
    assert "purchase.committed" in actions
    assert out["snapshot_id"] == plain.get("/v1/twin/state", headers=AUTH).json()["snapshot_id"]


def test_commit_requires_a_fix_when_not_allowed_and_refuses_stored_bills(plain, demo):
    warn = _check(plain, demo["PR-NW-RUSH"], 4000000)
    assert warn["decision"] == "warn"
    r = _commit(plain, warn["run_id"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "fix_required"
    stored = plain.post("/v1/twin/simulations", headers=AUTH, json={
        "proposed_action": {"bill_id": "BILL-LEASE-11", "pay_date": "2026-11-20"}}).json()
    r = _commit(plain, stored["run_id"], key="k-stored")
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_committable"


def test_funding_fix_commit_executes_transfers(plain, demo):
    d = _check(plain, demo["PR-NW-RUSH"], 4000000)
    fix = next(f for f in d["fixes"] if f["kind"] == "funding")
    out = _commit(plain, d["run_id"], fix["id"]).json()
    ex = out["executed_fix"]
    assert ex["fix_id"] == fix["id"] and ex["state"] == "simulated" and ex["cash_event_ids"]
    assert out["scheduled_date"] == "2026-11-20"
    assert [b.id for b in _purchases(plain)] == [out["purchase_id"]]
    actions = {e["action"] for e in plain.get("/v1/events", headers=AUTH).json()}
    assert {"approval.fix", "fix.executed", "cashevent.post", "purchase.committed"} <= actions
    # the purchase and the transfers are now part of the twin: the same request is a new snapshot
    assert plain.get("/v1/twin/state", headers=AUTH).json()["snapshot_id"] == out["snapshot_id"]


def test_four_step_script_with_real_stress_search(monkeypatch):
    """Laptops allow -> commit; Northwind rush warn (reschedule first) -> commit Nov 25; CNC block -> not
    committable; trade-show card charge allow before the rush is committed and warn after (one shared twin)."""
    monkeypatch.setenv("CASHGUARD_AUTO_STRESS", "1")
    monkeypatch.delenv("CASHGUARD_STRESS_BUDGET", raising=False)
    c = TestClient(create_app(Store()))
    sync = c.post("/v1/connections/fixture/sync", headers=AUTH).json()
    assert c.get("/v1/twin/stress-tests/latest?wait_s=120", headers=AUTH).json()["status"] == "completed"
    reqs = {p["id"]: p for p in c.get("/v1/purchases/demo", headers=AUTH).json()["requests"]}
    try:
        # 1. card: laptops
        laptops = _check(c, reqs["PR-LAPTOPS"])
        assert laptops["decision"] == "allow" and laptops["provenance"]["stress_set_source"] == "risk_search"
        pur1 = _commit(c, laptops["run_id"], key="s1").json()
        assert pur1["scheduled_date"] == "2026-11-20"
        # 4 (before 2). the same trade-show charge is fine with only the laptops committed
        before = _check(c, reqs["PR-TRADESHOW"])
        assert before["decision"] == "allow" and before["provenance"]["channel"] == "card"
        # 2. chat: Northwind rush
        rush = _check(c, reqs["PR-NW-RUSH"])
        assert rush["decision"] == "warn" and rush["fixes"][0]["kind"] == "reschedule"
        assert rush["fixes"][0]["pay_date"] == "2026-11-25"
        pur2 = _commit(c, rush["run_id"], rush["fixes"][0]["id"], key="s2").json()
        assert pur2["scheduled_date"] == "2026-11-25" and pur2["executed_fix"] is None
        bill = next(b for b in _purchases(c) if b.id == pur2["purchase_id"])
        assert str(bill.scheduled_date) == "2026-11-25" and bill.status == "scheduled"
        assert bill.due_date >= bill.scheduled_date and bill.vendor_id == "northwind"
        assert pur2["snapshot_id"] != pur1["snapshot_id"]
        # 3. checkout: CNC machine is blocked and cannot be committed
        cnc = _check(c, reqs["PR-CNC"])
        assert cnc["decision"] == "block" and cnc["fixes"] == []
        refused = _commit(c, cnc["run_id"], key="s3")
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "not_committable"
        # 4. card again: now over the line because of step 2's commitment
        after = _check(c, reqs["PR-TRADESHOW"])
        assert after["decision"] in ("warn", "block") and after["provenance"]["stress_set_source"] == "risk_search"
    finally:
        _CACHE.pop(sync["snapshot_id"], None)
