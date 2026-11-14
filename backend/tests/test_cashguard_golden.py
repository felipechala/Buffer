"""Golden decisions for the CashGuard demo company (fixtures/cashguard_demo.json).

These pin the demo story: baseline healthy; a $40k supplier payment today is safe only if
Acme pays on time; the paired shock (Acme late + reserve route down) is infeasible while each
shock alone is survivable; a $450k capex payment today has no feasible funding plan.
"""
from datetime import date

import pytest

from app.config import CASHGUARD_DEMO_FIXTURE
from app.engine.models import Scenario
from app.engine.optimizer import optimize_plan
from app.engine.simulator import simulate
from app.services.impact import ImpactRequest, analyze_bill_impact
from app.workflows.forecast import build_twin_from_store
from app.workflows.seed import seed_demo
from app.workflows.store import Store

ACME_LATE = Scenario(id="acme-late-3d", receipt_delays={"INV-4471": 3},
                     description="Acme invoice 4471 arrives 3 days late")
RESERVE_DOWN = Scenario(id="reserve-down", disabled_routes=["reserve-draw"],
                        description="reserve-draw route unavailable")
PAIRED = Scenario(id="acme-late-and-reserve-down", receipt_delays={"INV-4471": 3},
                  disabled_routes=["reserve-draw"])


@pytest.fixture
def store():
    s = Store()
    seed_demo(s, CASHGUARD_DEMO_FIXTURE)
    return s


def closing(result, account, day):
    return next(r.closing_cents for r in result.ledger if r.account_id == account and r.day == day)


def test_fixture_shape(store):
    twin = build_twin_from_store(store)
    assert twin.as_of == date(2026, 11, 20) and twin.horizon_days == 14
    assert [a.id for a in twin.accounts] == ["operating", "payroll", "tax", "reserve",
                                             "settlement", "intl_clearing"]
    # Paid history invoices are already in the snapshot: never forecast, never double-counted.
    assert {r.id for r in twin.receipts} == {"INV-4471", "INV-918", "INV-5002", "INV-EU-77"}
    assert twin.account("operating").observed_balance_cents == 44_000_000
    draft = next((o for o in twin.obligations if o.id == "BILL-EQUIP-9"), None)
    assert draft is None, "draft bills are not forecast obligations"


def test_baseline_is_healthy(store):
    result = simulate(build_twin_from_store(store))
    assert not result.metrics.breach
    assert closing(result, "operating", 4) == 32_700_000     # after payroll, Acme on time


def test_lease_today_is_safe_without_actions(store):
    impact = analyze_bill_impact(store, "BILL-LEASE-11", ImpactRequest(candidate_payment_days=[0]))
    [opt] = impact.options
    assert opt.status == "feasible" and opt.selected == "nominal"
    assert opt.nominal.actions == [] and opt.no_action_breach_rate == 0.0
    # Still clean under the Acme-late stress case.
    twin = build_twin_from_store(store, bill_pay_days={"BILL-LEASE-11": 0})
    assert not simulate(twin, ACME_LATE).metrics.breach


def test_northwind_today_needs_review_only_if_acme_is_late(store):
    twin = build_twin_from_store(store, bill_pay_days={"BILL-NW-4471": 0})
    assert not simulate(twin).metrics.breach                      # fine if Acme pays on time
    stressed = simulate(twin, ACME_LATE)
    assert stressed.metrics.breach
    first = stressed.metrics.first_breach
    assert (first.account_id, first.day) == ("operating", 4)     # payroll day, Nov 24
    assert closing(stressed, "operating", 4) == 6_700_000         # $33k below the $100k buffer

    impact = analyze_bill_impact(store, "BILL-NW-4471",
                                 ImpactRequest(candidate_payment_days=list(range(0, 8))))
    today = next(o for o in impact.options if o.pay_day == 0)
    assert today.status == "feasible" and today.selected == "robust"   # needs a funding plan
    assert today.no_action_breach_rate > 0.05
    assert today.robust.transfer_total_cents == 3_300_000 and today.robust.credit_draw_total_cents == 0

    # Rescheduling: the first safe later date without any funding is day 5 (Nov 25).
    no_action_days = [o.pay_day for o in impact.options
                      if o.selected == "nominal" and o.nominal.actions == []]
    assert min(no_action_days) == 5


def test_each_shock_alone_is_survivable_but_the_pair_is_not(store):
    twin = build_twin_from_store(store, bill_pay_days={"BILL-NW-4471": 0})
    assert optimize_plan(twin, ACME_LATE).status == "feasible"
    assert optimize_plan(twin, RESERVE_DOWN).status == "feasible"
    paired = optimize_plan(twin, PAIRED)
    assert paired.status == "infeasible"
    [v] = paired.violations
    assert (v.account_id, v.day, v.shortfall_cents) == ("operating", 4, 3_300_000)
    joined = " ".join(v.explanations)
    assert "reserve-draw is unavailable" in joined and "revolver funds after 5 day(s)" in joined


def test_large_capex_today_is_infeasible(store):
    impact = analyze_bill_impact(store, "BILL-EQUIP-9", ImpactRequest(candidate_payment_days=[0]))
    assert impact.status == "infeasible" and impact.recommended_pay_day is None
    [opt] = impact.options
    assert opt.first_buffer_breach_day == 0
    assert any(v.day == 0 for v in opt.nominal.violations)


def test_every_feasible_plan_replays_clean(store):
    twin = build_twin_from_store(store, bill_pay_days={"BILL-NW-4471": 0})
    plan = optimize_plan(twin, ACME_LATE)
    replay = simulate(twin, ACME_LATE, plan.actions)
    assert not replay.metrics.breach and not replay.rejected_actions
