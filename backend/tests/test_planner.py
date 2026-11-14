from app.planner import optimize_plan
from app.schemas import Scenario, TwinOverrides
from app.simulator import simulate
from app.twin import apply_overrides


def totals(plan):
    return plan.transfer_total_cents, plan.credit_draw_total_cents


def test_golden_response(golden):
    """Appendix A golden case: $120k transfer + $80k draw, $50k ending, $1,040 cost."""
    plan = optimize_plan(golden)
    assert plan.status == "feasible"
    assert totals(plan) == (12_000_000, 8_000_000)
    assert plan.modeled_cost_cents == 104_000
    check = simulate(golden, actions=plan.actions)
    op = [r for r in check.ledger if r.account_id == "operating" and r.day == 1][0]
    assert op.closing_cents == 5_000_000


def test_two_day_settlement_makes_golden_infeasible(golden):
    twin = apply_overrides(golden, TwinOverrides(route_settlement_days={"reserve->operating": 2}))
    plan = optimize_plan(twin)
    assert plan.status == "infeasible"
    [v] = plan.violations
    assert (v.account_id, v.day, v.shortfall_cents) == ("operating", 1, 10_000_000)
    joined = " ".join(v.explanations)
    assert "settles in 2 day(s)" in joined and "fully drawn" in joined


def test_disabled_route_changes_plan(golden):
    """Agent adaptation: losing the reserve route forces a different (here infeasible) answer."""
    plan = optimize_plan(golden, Scenario(id="no-reserve", disabled_routes=["reserve->operating"]))
    assert plan.status == "infeasible"
    assert all(a.kind == "credit_draw" for a in plan.actions)


def test_lower_route_cap_shifts_to_credit(golden):
    # Caps are per dispatch day: $50k/day on days 0 and 1 gives at most $100k by the deadline.
    twin = apply_overrides(golden, TwinOverrides(route_max_cents={"reserve->operating": 5_000_000}))
    plan = optimize_plan(twin)
    assert plan.status == "feasible"
    assert totals(plan) == (10_000_000, 10_000_000)
    assert sorted(a.day for a in plan.actions if a.kind == "transfer") == [0, 1]


def test_feasible_plans_respect_all_buffers_and_limits(demo):
    for sc in [Scenario(), Scenario(id="acme+2", receipt_delays={"acme-inv-4471": 2}),
               Scenario(id="reserve-down", disabled_routes=["reserve-draw"])]:
        plan = optimize_plan(demo, sc)
        assert plan.status == "feasible", sc.id
        check = simulate(demo, sc, plan.actions)
        assert not check.rejected_actions
        assert check.metrics.min_headroom_cents >= 0
        assert check.metrics.uncovered_obligations == []
        assert plan.credit_draw_total_cents <= demo.credit_line("revolver").available_cents
        assert not any(a.kind == "transfer" and a.route_id == "reserve-to-payroll" for a in plan.actions)


def test_paired_shock_is_infeasible_with_report(demo):
    sc = Scenario(id="pair", receipt_delays={"acme-inv-4471": 2}, disabled_routes=["reserve-draw"])
    plan = optimize_plan(demo, sc)
    assert plan.status == "infeasible"
    assert plan.violations and all(v.explanations for v in plan.violations)


def test_planner_is_deterministic(demo):
    a, b = optimize_plan(demo), optimize_plan(demo)
    assert a.id == b.id and a.actions == b.actions
