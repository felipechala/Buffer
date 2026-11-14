"""LLM explainer: grounding, fallbacks, and wiring into decide() -- fully offline."""
from types import SimpleNamespace

import pytest

from app.config import CASHGUARD_DEMO_FIXTURE
from app.services import explain
from app.services.decision import ProposedPayment, SimulationRequest, decide
from app.workflows.seed import seed_demo
from app.workflows.store import Store

PAYLOAD = {
    "decision": "warn",
    "payment": {"payee": "Northwind", "amount_usd": 40000.0, "requested_date": "2026-11-20"},
    "breaks": [{"account": "operating", "obligation": "payroll", "date": "2026-11-24", "shortfall_usd": 33000.0}],
    "scenario": "Acme pays 3d late",
    "fixes": [{"kind": "reschedule", "pay_date": "2026-11-25", "delay_days": 5, "cost_usd": 0.0,
               "summary": "Pay Northwind on Nov 25 instead of Nov 20", "actions": []}],
    "binding_constraints": [],
}
TEMPLATE = "template text"
GOOD = "Paying Northwind $40,000 on Nov 20 leaves payroll $33,000 short on Nov 24."


@pytest.fixture
def store():
    s = Store()
    seed_demo(s, CASHGUARD_DEMO_FIXTURE)
    return s


def test_ground_accepts_correct_sentence():
    t = "Paying Northwind $40,000 on Nov 20 leaves payroll $33,000 short on 2026-11-24; pay on November 25 instead."
    assert explain.ground(t, PAYLOAD)


@pytest.mark.parametrize("form", ["$40,000", "$40k", "$40K", "$40,000.00", "$0.04 million", "$0.04M"])
def test_ground_amount_forms(form):
    assert explain.ground(f"The payment of {form} is on Nov 20.", PAYLOAD)


def test_ground_rejects_invented_amount():
    assert not explain.ground("Paying $41,500 on Nov 20 is risky.", PAYLOAD)
    assert not explain.ground("You would be short $3.3k.", PAYLOAD)


def test_ground_rejects_invented_date():
    assert not explain.ground("Payroll is short on Nov 23.", PAYLOAD)
    assert not explain.ground("Pay on 2026-12-01 instead.", PAYLOAD)


def test_explain_without_key_is_template(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CASHGUARD_EXPLAIN", "1")
    e = explain.explain(PAYLOAD, TEMPLATE)
    assert e.source == "template" and e.text == TEMPLATE


def _fake_client(text, stop_reason="end_turn", boom=None):
    def create(**kw):
        if boom:
            raise boom
        return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])
    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_explain_ungrounded_llm_text_falls_back(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("CASHGUARD_EXPLAIN", "1")
    monkeypatch.setattr(explain, "_get_client", lambda: _fake_client("Payroll is short $99,000 on Nov 24."))
    e = explain.explain(PAYLOAD, TEMPLATE)
    assert e.source == "template" and e.text == TEMPLATE and e.reason == "ungrounded"


def test_explain_grounded_llm_text_is_used(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("CASHGUARD_EXPLAIN", "1")
    monkeypatch.setattr(explain, "_get_client", lambda: _fake_client(GOOD))
    e = explain.explain(PAYLOAD, TEMPLATE)
    assert e.source == "llm" and e.text == GOOD


def test_explain_exception_and_disabled_fall_back(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("CASHGUARD_EXPLAIN", "1")
    monkeypatch.setattr(explain, "_get_client", lambda: _fake_client("", boom=TimeoutError("slow")))
    assert explain.explain(PAYLOAD, TEMPLATE).source == "template"
    monkeypatch.setenv("CASHGUARD_EXPLAIN", "0")
    monkeypatch.setattr(explain, "_get_client", lambda: pytest.fail("must not call the API when disabled"))
    assert explain.explain(PAYLOAD, TEMPLATE).reason == "disabled"


def test_decide_template_when_disabled(store):
    d = decide(store, SimulationRequest(proposed_action=ProposedPayment(bill_id="BILL-NW-4471", pay_date="2026-11-20")))
    assert d.provenance.explanation_source == "template" and "$33,000" in d.explanation


def test_decide_is_template_and_explanation_is_async(store, monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import CASHGUARD_API_KEY
    from app.main import create_app
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("CASHGUARD_EXPLAIN", "1")
    explain._cache.clear()
    calls = []

    def client():
        calls.append(1)
        return _fake_client(GOOD)
    monkeypatch.setattr(explain, "_get_client", client)
    req = SimulationRequest(proposed_action=ProposedPayment(bill_id="BILL-NW-4471", pay_date="2026-11-20"))
    d = decide(store, req)
    assert d.provenance.explanation_source == "template" and d.explanation != GOOD and not calls
    assert d.explanation_url == f"/v1/runs/{d.run_id}/explanation"
    decide(store, req, persist=False)          # warm-up never calls the LLM nor sets a url
    assert not calls

    auth = {"Authorization": f"Bearer {CASHGUARD_API_KEY}"}
    c = TestClient(create_app(store))
    e1 = c.get(d.explanation_url, headers=auth).json()
    e2 = c.get(d.explanation_url, headers=auth).json()
    assert e1["source"] == "llm" and e1["text"] == GOOD and e1["run_id"] == d.run_id and e1["model"]
    assert e2 == e1 and len(calls) == 1
    assert c.get("/v1/runs/run_nope/explanation", headers=auth).status_code == 404

    explain._cache.clear()
    monkeypatch.setattr(explain, "_get_client", lambda: _fake_client("", boom=TimeoutError("slow")))
    e3 = c.get(d.explanation_url, headers=auth)
    assert e3.status_code == 200 and e3.json()["source"] == "template" and e3.json()["text"] == d.explanation
    explain._cache.clear()


def test_explain_truncated_reply_falls_back_and_is_not_cached(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("CASHGUARD_EXPLAIN", "1")
    cut = "Paying Northwind $40,000 on Nov 20 leaves payroll $33,000 short on Nov 24, so it needs to be rescheduled or partially fun"
    monkeypatch.setattr(explain, "_get_client", lambda: _fake_client(cut, stop_reason="max_tokens"))
    e = explain.explain(PAYLOAD, TEMPLATE)
    assert e.source == "template" and e.text == TEMPLATE and e.reason == "truncated"

    explain._cache.clear()
    explain._remember("digest-x", e)            # template results must never enter the cache
    assert "digest-x" not in explain._cache
    explain._remember("digest-y", explain.Explanation(text=GOOD, source="llm"))
    assert explain._cache["digest-y"].text == GOOD
    explain._cache.clear()
