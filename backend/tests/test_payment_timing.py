"""Learned payment-timing model: deterministic, honest model card, wired into the agent and the API."""
from fastapi.testclient import TestClient

from app.agent import ops
from app.config import CASHGUARD_API_KEY, CASHGUARD_DEMO_FIXTURE
from app.main import create_app
from app.ml import payment_timing as pt
from app.workflows.seed import seed_demo
from app.workflows.store import Store


def _store():
    s = Store()
    seed_demo(s, CASHGUARD_DEMO_FIXTURE)
    return s


def test_model_is_deterministic_and_reports_against_baseline():
    pt._CACHE.clear()
    a = pt.train(_store())
    b = pt.train(_store())
    assert a.card == b.card
    c = a.card
    assert c.trained_on_real == 7 and c.trained_on_synthetic > 0 and c.holdout_rows > 0
    assert c.improvement_pct == round(100 * (c.holdout_log_loss_histogram - c.holdout_log_loss_model)
                                      / c.holdout_log_loss_histogram, 1)
    assert "SYNTHETIC" in c.note


def test_predictions_cover_open_invoices():
    s = _store()
    preds = pt.get_model(s).predict(s)
    assert {p.invoice_id for p in preds} == {"INV-4471", "INV-918", "INV-5002", "INV-EU-77"}
    for p in preds:
        assert abs(sum(p.distribution.values()) - 1) < 1e-3 and 0 <= p.p_late <= 1


def test_agent_tool_and_api():
    s = _store()
    run, _ = ops.start_run(s)
    ctx = ops.OpsContext(s, run)
    tool = next(t for t in ctx.tools_for("cash-analyst") if t.name == "predict_collections")
    out = tool.invoke({})
    assert out["invoices"] and "improvement_pct" in out["model_card"] and run.steps[-1].agent == "cash-analyst"
    with TestClient(create_app(_store())) as c:
        r = c.get("/v1/ml/payment-timing", headers={"Authorization": f"Bearer {CASHGUARD_API_KEY}"})
        assert r.status_code == 200 and len(r.json()["predictions"]) == 4
