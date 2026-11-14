"""deepagents layer: fallback + fake-agent paths, fully offline (the real agent is never constructed)."""
from fastapi.testclient import TestClient

from app.agent import deep
from app.agent.runs import execute_run, queue_run
from app.config import CASHGUARD_API_KEY, CASHGUARD_DEMO_FIXTURE
from app.main import create_app
from app.services.stress import _CACHE
from app.workflows.forecast import build_twin_from_store
from app.workflows.seed import seed_demo
from app.workflows.store import Store

AUTH = {"Authorization": f"Bearer {CASHGUARD_API_KEY}"}
BUDGET = 20


def _store():
    s = Store()
    seed_demo(s, CASHGUARD_DEMO_FIXTURE)
    return s


def _elites(run):
    return [(e.scenario.id, e.worst_shortfall_cents, e.fixable) for e in run.elites]


def _deterministic(budget=BUDGET):
    s = _store()
    run, _ = queue_run(s, build_twin_from_store(s).snapshot_id, seed=0, budget=budget)
    return execute_run(s, run.id)


def _queue_deep(s):
    run, _ = queue_run(s, build_twin_from_store(s).snapshot_id, seed=0, budget=BUDGET, mode="deepagent")
    return run


def fake_agent(ctx):
    ctx.list_shocks()
    ctx.search_shocks(budget=10, focus="receivables")
    ctx.search_shocks(budget=10, focus="routes")
    ctx.simulate({"delays": {}})
    return {"text": "Fake agent summary.", "llm_calls": 3}


def test_fallback_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    det = _deterministic()
    s = _store()
    out = deep.execute_deep(s, _queue_deep(s).id)
    assert out.status == "completed" and out.mode == "deepagent" and out.agent == "fallback"
    assert "ANTHROPIC_API_KEY" in out.agent_reason and out.narrative == []
    assert _elites(out) == _elites(det) and out.id != det.id


def test_fallback_when_deepagents_missing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(deep, "deepagents_available", lambda: False)
    s = _store()
    out = deep.execute_deep(s, _queue_deep(s).id)
    assert out.agent == "fallback" and "not installed" in out.agent_reason and out.elites


def test_fake_agent_populates_narrative_and_keeps_engine_elites():
    det = _deterministic()
    s = _store()
    out = deep.execute_deep(s, _queue_deep(s).id, agent_runner=fake_agent)
    assert out.agent == "deepagent" and out.agent_meta["llm_calls"] == 3 and out.agent_meta["tool_calls"] == 4
    tools = [st["tool"] for st in out.narrative]
    assert tools[:4] == ["list_shocks", "search_shocks", "search_shocks", "simulate"] and tools[-1] is None
    assert out.narrative[1]["result"]["focus"] == "receivables"
    assert _elites(out) == _elites(det)


def test_agent_error_and_tool_cap_fall_back():
    s = _store()

    def boom(ctx):
        ctx.list_shocks()
        raise RuntimeError("model exploded")
    info = deep.explore(s, seed=0, agent_runner=boom)
    assert info["agent"] == "fallback" and "model exploded" in info["reason"] and info["tool_calls"] == 1

    def greedy(ctx):
        return {"text": "", "llm_calls": 1, "results": [ctx.list_shocks() for _ in range(5)]}
    info = deep.explore(s, seed=0, agent_runner=greedy, max_calls=2)
    assert info["agent"] == "deepagent" and info["tool_calls"] == 2


def test_endpoint_modes(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(deep, "deepagents_available", lambda: True)
    monkeypatch.setattr(deep, "_run_agent", fake_agent)
    monkeypatch.setattr("app.agent.runs.warm_decisions", lambda store: 0)
    c = TestClient(create_app(Store()))
    c.post("/v1/connections/fixture/sync", headers=AUTH)
    det = c.post("/v1/twin/stress-tests", headers=AUTH, json={"budget": BUDGET}).json()
    assert det["mode"] == "deterministic"
    deep_id = c.post("/v1/twin/stress-tests", headers=AUTH, json={"budget": BUDGET, "mode": "deepagent"}).json()["run_id"]
    d = c.get(f"/v1/twin/stress-tests/{det['run_id']}?wait_s=0", headers=AUTH).json()
    g = c.get(f"/v1/twin/stress-tests/{deep_id}?wait_s=0", headers=AUTH).json()
    assert (d["mode"], d["agent"], d["narrative"]) == ("deterministic", "none", [])
    assert (g["mode"], g["agent"]) == ("deepagent", "deepagent") and len(g["narrative"]) == 5
    assert [e["worst_shortfall_cents"] for e in g["elites"]] == [e["worst_shortfall_cents"] for e in d["elites"]]
    _CACHE.clear()
