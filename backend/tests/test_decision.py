"""Decision contract on the CashGuard demo company: allow / warn / block + verified fixes."""
from datetime import date

import pytest

from app.config import CASHGUARD_DEMO_FIXTURE
from app.engine.models import Scenario
from app.engine.simulator import simulate
from app.services.decision import ProposedPayment, SimulationRequest, decide
from app.services.fixes import Fix
from app.services.stress import get_stress_set
from app.workflows.forecast import build_twin_from_store
from app.workflows.models import AnalysisRun
from app.workflows.seed import seed_demo
from app.workflows.store import Store


@pytest.fixture
def store():
    s = Store()
    seed_demo(s, CASHGUARD_DEMO_FIXTURE)
    return s


def check(store, **action):
    return decide(store, SimulationRequest(proposed_action=ProposedPayment(**action)))


def test_default_stress_set_contains_acme_late(store):
    twin = build_twin_from_store(store)
    ids = {s.id for s in get_stress_set(twin, 0.05).scenarios}
    assert "acme-late-3d" in ids and any(i.startswith("adverse") for i in ids)


def test_lease_today_is_allowed(store):
    d = check(store, bill_id="BILL-LEASE-11", pay_date=date(2026, 11, 20))
    assert d.decision == "allow" and d.safe and d.fixes == [] and d.breaks == []
    assert d.provenance.scenarios_checked >= 2 and d.latency_ms >= 0


def test_northwind_today_warns_with_three_verified_fixes(store):
    d = check(store, bill_id="BILL-NW-4471", pay_date=date(2026, 11, 20))
    assert d.decision == "warn" and not d.safe
    [b] = d.breaks[:1]
    assert (b.account, b.date, b.shortfall_cents) == ("operating", date(2026, 11, 24), 3_300_000)
    assert b.obligation in ("payroll", "Employees (payroll run)") and b.scenario.id == "acme-late-3d"

    kinds = [f.kind for f in d.fixes]
    assert kinds[0] == "reschedule" and d.fixes[0].pay_date == date(2026, 11, 25)
    assert d.fixes[0].cost_cents == 0 and d.fixes[0].actions[0].kind == "schedule_bill"
    funding = [f for f in d.fixes if f.kind == "funding"]
    assert funding, "expected a same-day funding fix"
    transfer = funding[0].actions[0]
    assert transfer.kind == "transfer" and transfer.route_id == "reserve-draw"
    assert transfer.amount_cents == 3_300_000 and transfer.arrival_day <= 4
    assert all(f.verified for f in d.fixes)
    assert d.fixes == sorted(d.fixes, key=lambda f: (f.cost_cents, f.delay_days, f.held_out.breach_rate))
    assert "Acme" in d.explanation and "$33,000" in d.explanation
    assert set(d.curves) >= {"baseline", "stressed", *[f.id for f in d.fixes]}


def test_fixes_replay_clean_under_every_stress_scenario(store):
    d = check(store, bill_id="BILL-NW-4471", pay_date=date(2026, 11, 20))
    twin = build_twin_from_store(store)
    stress = get_stress_set(twin, 0.05).scenarios
    for f in d.fixes:
        t = build_twin_from_store(store, bill_pay_days={"BILL-NW-4471": f.pay_day})
        for s in [Scenario(), *stress]:
            r = simulate(t, s, f.plan_actions)
            assert not r.metrics.breach and not r.rejected_actions, (f.id, s.id)


def test_large_capex_today_is_blocked(store):
    d = check(store, bill_id="BILL-EQUIP-9", pay_date=date(2026, 11, 20))
    assert d.decision == "block" and d.fixes == []
    assert d.breaks and d.breaks[0].date == date(2026, 11, 20) and d.breaks[0].scenario.id == "baseline"
    assert d.infeasibility and any("reserve-draw" in line for line in d.constraint_report)


def test_adhoc_payment_without_a_stored_bill(store):
    d = check(store, amount_cents=4_000_000, from_account="operating", payee="Northwind Supplies",
              pay_date=date(2026, 11, 20), latest_date=date(2026, 11, 27))
    assert d.decision == "warn" and d.payee == "Northwind Supplies" and d.bill_id.startswith("adhoc-")
    # The ephemeral bill never entered the store or the forecast.
    assert d.bill_id not in {o.id for o in build_twin_from_store(store).obligations}


def test_judge_scenario_override_is_checked_first(store):
    req = SimulationRequest(
        proposed_action=ProposedPayment(bill_id="BILL-LEASE-11", pay_date=date(2026, 11, 20)),
        scenario=Scenario(id="judge", receipt_delays={"INV-4471": 3, "INV-918": 2, "INV-5002": 2},
                          unexpected_outflows=[{"account_id": "operating", "day": 3,
                                                "amount_cents": 1_000_000, "description": "surprise"}]))
    d = decide(store, req)
    assert d.decision in ("warn", "block") and d.breaks[0].scenario.id == "judge"


def test_decision_is_deterministic_and_persisted(store):
    a = check(store, bill_id="BILL-NW-4471", pay_date=date(2026, 11, 20))
    b = check(store, bill_id="BILL-NW-4471", pay_date=date(2026, 11, 20))
    assert a.provenance.input_digest == b.provenance.input_digest and a.run_id == b.run_id
    assert a.decision == b.decision and [f.id for f in a.fixes] == [f.id for f in b.fixes]
    assert store.get(AnalysisRun, a.run_id) is not None
    stored = store.get(Fix, a.fixes[0].id)
    assert stored is not None and stored.snapshot_id == a.provenance.snapshot_id


def test_stale_snapshot_is_rejected(store):
    with pytest.raises(Exception) as e:
        decide(store, SimulationRequest(snapshot_id="snap_nope",
                                        proposed_action=ProposedPayment(bill_id="BILL-LEASE-11")))
    assert getattr(e.value, "status", None) == 409
