"""Treasury deepagent: tools, approval gate, learned preferences. Fully offline (fake agent runner)."""
import pytest
from fastapi.testclient import TestClient

from app.agent import ops
from app.config import CASHGUARD_API_KEY, CASHGUARD_DEMO_FIXTURE
from app.main import create_app
from app.services.fixes import Fix
from app.workflows.models import APBill
from app.workflows.seed import seed_demo
from app.workflows.state import WorkflowError
from app.workflows.store import Store

AUTH = {"Authorization": f"Bearer {CASHGUARD_API_KEY}"}
DUE = {"BILL-NW-4471": "2026-11-27", "BILL-LEASE-11": "2026-11-28", "BILL-INITECH-2210": "2026-11-26",
       "BILL-CLOUD-11": "2026-11-26", "BILL-MKT-11": "2026-11-30"}
ALL_DISCOUNTS = {**DUE, "BILL-NW-4471": "2026-11-21", "BILL-INITECH-2210": "2026-11-22",
                 "BILL-CLOUD-11": "2026-11-21"}


@pytest.fixture(autouse=True)
def _clear_live():
    ops._LIVE.clear()
    yield
    ops._LIVE.clear()


def _store():
    s = Store()
    seed_demo(s, CASHGUARD_DEMO_FIXTURE)
    return s


def _ctx(s):
    run, _ = ops.start_run(s)
    return ops.OpsContext(s, run)


def fake_agent(schedule):
    def runner(ctx, prompt):
        ctx._call("buffer", "read_preferences", {}, lambda: {"preferences": [p.text for p in ctx.prefs]})
        ctx._call("buffer", "get_cash_position", {}, ctx.cash_position)
        ctx._call("payables-planner", "list_open_bills", {}, ctx.open_bills)
        base = ctx._call("payables-planner", "check_payment_run", {}, lambda: ctx.check_run(DUE, []))
        best = ctx._call("payables-planner", "check_payment_run", {}, lambda: ctx.check_run(schedule, []))
        pick = best if best.get("verified") else base
        ctx._call("buffer", "submit_proposal", {}, lambda: ctx.submit(
            pick["draft_id"], "Fake rationale.", [{"bill_id": "BILL-EQUIP-9", "reason": "draft"}]))
        return {"text": "Submitted.", "prompt": prompt}
    return runner


# ------------------------------------------------------------------ tools
def test_discounts_vs_funding_tradeoff():
    ctx = _ctx(_store())
    base = ctx.check_run(DUE, [])
    assert base["verified"] and base["status"] == "no_funding_needed" and base["net_benefit_usd"] == 0
    early = ctx.check_run(ALL_DISCOUNTS, [])
    assert early["verified"] and early["status"] == "funded"
    assert early["discounts_captured_usd"] == 3250.0
    assert early["net_benefit_usd"] == pytest.approx(3250.0 - early["funding_cost_usd"])
    assert all(a["kind"] == "transfer" for a in early["funding_actions"])


def test_schedule_rules_enforced():
    ctx = _ctx(_store())
    assert "nondeferrable" in ctx.check_run({"PAYROLL-1124": "2026-11-25"}, [])["problems"][0]
    assert "draft" in ctx.check_run({"BILL-EQUIP-9": "2026-11-20"}, [])["problems"][0]
    assert "legal window" in ctx.check_run({"BILL-MKT-11": "2026-12-05"}, [])["problems"][0]
    assert "bad date" in ctx.check_run({"BILL-MKT-11": "next tuesday"}, [])["problems"][0]


def test_protected_account_is_enforced():
    s = _store()
    ctx = _ctx(s)
    ctx.pref_flags = ["protect:reserve"]
    r = ctx.check_run(ALL_DISCOUNTS, [])
    assert not r["verified"] and r["protected_accounts"] == ["reserve"]


def test_preference_flags():
    accts = [("reserve", "Strategic Reserve"), ("operating", "Operating")]
    assert ops.preference_flags("Don't drain the strategic reserve", accts) == ["protect:reserve"]
    assert ops.preference_flags("no credit line draws please") == ["no_credit"]
    assert ops.preference_flags("avoid transfers between accounts") == ["no_transfers"]


def test_unverified_draft_cannot_be_submitted():
    ctx = _ctx(_store())
    assert "error" in ctx.submit("draft-99", "x", None)


def test_tool_budget():
    s = _store()
    run, _ = ops.start_run(s)
    ctx = ops.OpsContext(s, run, max_calls=1)
    ctx._call("buffer", "list_open_bills", {}, ctx.open_bills)
    assert "budget" in ctx._call("buffer", "list_open_bills", {}, ctx.open_bills)["error"]


# ------------------------------------------------------------------ lifecycle
def test_run_to_proposal_and_approve_executes():
    s = _store()
    run, needs = ops.start_run(s)
    assert needs
    out = ops.execute_run(s, run.id, agent_runner=fake_agent(ALL_DISCOUNTS))
    assert out.status == "awaiting_approval" and out.proposal.verified
    assert out.proposal.discounts_cents == 325000 and out.proposal.held_bills[0]["bill_id"] == "BILL-EQUIP-9"
    assert [st.agent for st in out.steps][:3] == ["buffer", "buffer", "payables-planner"]
    with pytest.raises(WorkflowError):
        ops.approve_run(s, run.id, approved_by="u_omar")              # not a finance lead
    done = ops.approve_run(s, run.id, approved_by="u_maya")
    assert done.status == "executed" and done.execution["mode"] == "simulated"
    assert s.get(APBill, "BILL-INITECH-2210").scheduled_date.isoformat() == "2026-11-22"
    assert s.get(APBill, "BILL-NW-4471").scheduled_date.isoformat() == "2026-11-21"
    assert s.get(Fix, f"agentfix-{run.id}") is not None
    assert ops.approve_run(s, run.id, approved_by="u_maya").execution == done.execution   # idempotent


def test_stale_proposal_refused():
    s = _store()
    run, _ = ops.start_run(s)
    ops.execute_run(s, run.id, agent_runner=fake_agent(ALL_DISCOUNTS))
    bill = s.get(APBill, "BILL-MKT-11")
    s.save(bill.model_copy(update={"amount_cents": bill.amount_cents + 100}), actor="t", action="t")
    with pytest.raises(WorkflowError) as e:
        ops.approve_run(s, run.id, approved_by="u_maya")
    assert e.value.code == "stale_proposal"


def test_reject_learns_preference_and_revises():
    s = _store()
    run, _ = ops.start_run(s)
    ops.execute_run(s, run.id, agent_runner=fake_agent(ALL_DISCOUNTS))
    rejected, rev, needs = ops.reject_run(s, run.id, rejected_by="u_maya",
                                          reason="Keep the strategic reserve untouched this week")
    assert rejected.status == "rejected" and needs and rev.parent_id == run.id
    assert ops.preferences(s)[0].flags == ["protect:reserve"]
    prompt = ops.build_prompt(s, rev)
    assert "REJECTED" in prompt and "strategic reserve" in prompt
    out = ops.execute_run(s, rev.id, agent_runner=fake_agent(ALL_DISCOUNTS))
    # the discount schedule needs the reserve, so only the due-date plan is verifiable now
    assert out.status == "awaiting_approval" and out.proposal.funding == [] and out.proposal.discounts_cents == 0


def test_no_key_fails_clearly(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = _store()
    run, _ = ops.start_run(s)
    out = ops.execute_run(s, run.id)
    assert out.status == "failed" and "ANTHROPIC_API_KEY" in out.error


def test_agent_without_proposal_fails():
    s = _store()
    run, _ = ops.start_run(s)
    out = ops.execute_run(s, run.id, agent_runner=lambda ctx, p: {"text": "I give up."})
    assert out.status == "failed" and "without submitting" in out.error


# ------------------------------------------------------------------ API
def test_api_flow(monkeypatch):
    monkeypatch.setenv("CASHGUARD_AUTO_STRESS", "0")
    monkeypatch.setattr(ops, "_run_deep_agent", fake_agent(ALL_DISCOUNTS))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(ops, "deepagents_available", lambda: True)
    app = create_app(Store())
    with TestClient(app) as c:
        assert c.post("/v1/connections/fixture/sync?reset=true", headers=AUTH).status_code == 200
        r = c.post("/v1/agent/runs", headers=AUTH, json={})
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        run = c.get(f"/v1/agent/runs/{run_id}", headers=AUTH).json()
        assert run["status"] == "awaiting_approval"
        rej = c.post(f"/v1/agent/runs/{run_id}/reject", headers=AUTH,
                     json={"rejected_by": "u_maya", "reason": "don't touch the reserve"}).json()
        rev = c.get(f"/v1/agent/runs/{rej['revision_run_id']}", headers=AUTH).json()
        assert rev["status"] == "awaiting_approval" and rev["parent_id"] == run_id
        assert c.get("/v1/agent/preferences", headers=AUTH).json()[0]["flags"] == ["protect:reserve"]
        ok = c.post(f"/v1/agent/runs/{rev['id']}/approve", headers=AUTH, json={"approved_by": "u_maya"})
        assert ok.status_code == 200 and ok.json()["status"] == "executed"
        assert c.get("/v1/agent/runs", headers=AUTH).json()[0]["id"] in (run_id, rev["id"])
        assert c.delete("/v1/agent/preferences", headers=AUTH).json() == {"deleted": 1}
