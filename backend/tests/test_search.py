"""Adaptive risk search on the CashGuard demo company (budget 120, deterministic)."""
import pytest
from fastapi.testclient import TestClient

from app.agent.runs import execute_run, queue_run, warm_decisions
from app.config import CASHGUARD_API_KEY, CASHGUARD_DEMO_FIXTURE
from app.engine.optimizer import optimize_plan
from app.main import create_app
from app.services.decision import SimulationRequest, decide
from app.services.stress import _CACHE
from app.workflows.forecast import build_twin_from_store
from app.workflows.seed import seed_demo
from app.workflows.store import Store

AUTH = {"Authorization": f"Bearer {CASHGUARD_API_KEY}"}
NW = SimulationRequest(proposed_action={"bill_id": "BILL-NW-4471", "pay_date": "2026-11-20"})


def _store():
    s = Store()
    seed_demo(s, CASHGUARD_DEMO_FIXTURE)
    return s


@pytest.fixture(scope="module")
def searched():
    s = _store()
    snap = build_twin_from_store(s).snapshot_id
    _CACHE.pop(snap, None)
    run, _ = queue_run(s, snap, budget=120)
    done = execute_run(s, run.id)
    yield s, done
    _CACHE.pop(snap, None)          # do not leak the discovered set into other test modules


def test_run_completes_and_trace_has_mutations(searched):
    _, run = searched
    assert run.status == "completed" and run.error is None and len(run.trace) == 120
    assert run.trace[0]["reason"] == "random" and all(t["reason"] == "random" for t in run.trace[:20])
    assert any(t["reason"].startswith("mutate_of=shock-") for t in run.trace)
    assert 1 <= len(run.elites) <= 10 and run.runtime_ms is not None


def test_finds_the_paired_shock_and_marks_it_unfixable(searched):
    s, run = searched
    paired = [e for e in run.elites if e.shock.delays.get("acme", 0) >= 2
              and e.shock.disabled_route == "reserve-draw" and not e.shock.disabled_credit]
    assert paired and all(not e.fixable for e in paired) and paired[0].infeasibility
    twin = build_twin_from_store(s, bill_pay_days={"BILL-NW-4471": 0})      # the $40k Northwind paid today
    assert optimize_plan(twin, paired[0].scenario).status == "infeasible"
    # single shocks stay fixable
    assert any(e.fixable for e in run.elites)


def test_adaptive_is_no_slower_than_random(searched):
    _, run = searched
    a, r = run.adaptive, run.random_baseline
    assert a["evaluations"] == r["evaluations"] == 120
    assert a["first_breach_found_at"] is not None and a["first_breach_found_at"] <= r["first_breach_found_at"]
    assert a["first_unfixable_found_at"] is not None
    assert a["first_unfixable_found_at"] <= (r["first_unfixable_found_at"] or 10**9)
    assert a["unique_breaches"] >= r["unique_breaches"]


def test_deterministic_for_same_seed(searched):
    _, run = searched
    s2 = _store()
    again = execute_run(s2, queue_run(s2, run.snapshot_id, budget=120)[0].id)
    assert [e.shock.key() for e in again.elites] == [e.shock.key() for e in run.elites]
    assert again.trace == run.trace


def test_decision_uses_discovered_set(searched):
    s, run = searched
    assert run.stress_set_id and run.stress_set_id.startswith("stress-search-")
    d = decide(s, NW, persist=False)
    assert d.provenance.stress_set_source == "risk_search" and d.decision == "warn"
    assert d.breaks and d.breaks[0].scenario.id.startswith("shock-")
    assert d.fixes, "a discovered set must still leave verified fixes"


def test_warm_writes_nothing(searched):
    s, _ = searched
    audit_before, runs_before = len(s.audit(limit=100_000)), len(s.list(__import__("app.workflows.models", fromlist=["AnalysisRun"]).AnalysisRun))
    assert warm_decisions(s) >= 3
    assert len(s.audit(limit=100_000)) == audit_before
    assert len(s.list(__import__("app.workflows.models", fromlist=["AnalysisRun"]).AnalysisRun)) == runs_before


def test_endpoints_and_auto_stress_on_sync(monkeypatch):
    monkeypatch.setenv("CASHGUARD_AUTO_STRESS", "1")
    monkeypatch.setenv("CASHGUARD_STRESS_BUDGET", "40")
    monkeypatch.setattr("app.agent.runs.warm_decisions", lambda store: 0)     # warming has its own test
    app = create_app(Store())
    c = TestClient(app)
    sync = c.post("/v1/connections/fixture/sync", headers=AUTH).json()
    assert sync["stress_run_id"].startswith("stress_")
    run = c.get("/v1/twin/stress-tests/latest", headers=AUTH).json()
    assert run["status"] == "completed" and run["run_id"] == sync["stress_run_id"] and run["budget"] == 40
    assert run["adaptive"]["evaluations"] == 40 and run["findings"] and "scenarios" in run
    actions = {e["action"] for e in c.get("/v1/events", headers=AUTH).json()}
    assert "simulation.decide" not in actions and "fix.create" not in actions     # warming is silent
    again = c.post("/v1/connections/fixture/sync", headers=AUTH).json()           # same snapshot: run is reused
    assert again["stress_run_id"] == sync["stress_run_id"]
    started = c.post("/v1/twin/stress-tests", headers=AUTH, json={"budget": 20, "seed": 3})
    assert started.status_code == 202
    got = c.get(f"/v1/twin/stress-tests/{started.json()['run_id']}", headers=AUTH).json()
    assert got["status"] == "completed" and got["seed"] == 3
    _CACHE.pop(sync["snapshot_id"], None)
