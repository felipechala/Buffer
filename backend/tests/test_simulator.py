import pytest
from pydantic import ValidationError

from app.schemas import CreditDrawAction, Scenario, TransferAction, UnexpectedOutflow
from app.simulator import simulate


def closing(result, account_id):
    return [r.closing_cents for r in result.ledger if r.account_id == account_id]


def test_golden_no_action_shortfall(golden):
    """Appendix A: without intervention operating falls to -$150,000 after payroll."""
    res = simulate(golden)
    assert closing(res, "operating") == [10_000_000, -15_000_000, -15_000_000]
    assert res.metrics.first_breach.account_id == "operating"
    assert res.metrics.first_breach.day == 1
    assert res.metrics.uncovered_obligations == ["payroll-1"]
    assert res.metrics.uncovered_mandatory_cents == 25_000_000


def test_zero_fee_transfer_conserves_cash_including_in_transit(make_twin):
    twin = make_twin(transfer_routes=[{"id": "slow", "from": "a", "to": "b",
                                       "settlement_days": 2, "max_amount": 1000}])
    res = simulate(twin, actions=[TransferAction(route_id="slow", day=0, amount_cents=40_000)])
    assert res.total_cash_by_day == [100_000] * 5
    assert res.in_transit_by_day == [40_000, 40_000, 0, 0, 0]
    assert res.metrics.fees_cents == 0


def test_delayed_transfer_unavailable_until_settlement(make_twin):
    twin = make_twin(transfer_routes=[{"id": "slow", "from": "a", "to": "b",
                                       "settlement_days": 2, "max_amount": 1000}])
    res = simulate(twin, actions=[TransferAction(route_id="slow", day=0, amount_cents=40_000)])
    assert closing(res, "a") == [60_000] * 5
    assert closing(res, "b") == [0, 0, 40_000, 40_000, 40_000]


def test_two_day_transfer_cannot_cover_tomorrows_payment(make_twin):
    twin = make_twin(
        transfer_routes=[{"id": "slow", "from": "a", "to": "b", "settlement_days": 2, "max_amount": 1000}],
        obligations=[{"id": "bill", "account": "b", "amount": 300, "due_day": 1}],
    )
    res = simulate(twin, actions=[TransferAction(route_id="slow", day=0, amount_cents=30_000)])
    outcome = res.obligations[0]
    assert not outcome.covered
    assert res.metrics.first_breach.day == 1


def test_same_day_transfer_lands_before_obligation(make_twin):
    twin = make_twin(obligations=[{"id": "bill", "account": "b", "amount": 300, "due_day": 0}])
    res = simulate(twin, actions=[TransferAction(route_id="a->b", day=0, amount_cents=30_000)])
    assert res.obligations[0].covered
    assert not res.metrics.breach


def test_buffer_shortfall_timestamped(make_twin):
    twin = make_twin(accounts=[{"id": "a", "observed_balance": 1000, "min_buffer": 500},
                               {"id": "b", "observed_balance": 0}],
                     obligations=[{"id": "bill", "account": "a", "amount": 600, "due_day": 3}])
    res = simulate(twin)
    assert res.metrics.first_breach.model_dump() == {"account_id": "a", "day": 3, "shortfall_cents": 10_000}
    assert res.obligations[0].covered and res.obligations[0].buffer_breached


def test_deterministic_and_no_input_mutation(demo):
    before = demo.model_dump()
    sc = Scenario(id="s", receipt_delays={"acme-inv-4471": 2})
    r1 = simulate(demo, sc)
    r2 = simulate(demo, sc)
    assert r1.model_dump() == r2.model_dump()
    assert demo.model_dump() == before


def test_receipt_delay_and_haircut(make_twin):
    twin = make_twin(expected_receipts=[{"id": "r", "account": "b", "amount": 100, "expected_day": 1}])
    base = simulate(twin)
    assert closing(base, "b") == [0, 10_000, 10_000, 10_000, 10_000]
    late = simulate(twin, Scenario(receipt_delays={"r": 2}, receipt_haircuts={"r": 0.25}))
    assert closing(late, "b") == [0, 0, 0, 7_500, 7_500]
    gone = simulate(twin, Scenario(receipt_delays={"r": 10}))
    assert gone.receipts_beyond_horizon == ["r"]


def test_unexpected_outflow(make_twin):
    res = simulate(make_twin(), Scenario(unexpected_outflows=[
        UnexpectedOutflow(account_id="a", day=2, amount_cents=25_000)]))
    assert closing(res, "a") == [100_000, 100_000, 75_000, 75_000, 75_000]


def test_invalid_actions_rejected(make_twin):
    twin = make_twin(credit_lines=[{"id": "loc", "account": "a", "available_limit": 100}])
    res = simulate(twin, Scenario(disabled_routes=["a->b"]), actions=[
        TransferAction(route_id="a->b", day=0, amount_cents=1),
        TransferAction(route_id="ghost", day=0, amount_cents=1),
        CreditDrawAction(credit_line_id="loc", day=0, amount_cents=10_001),
        CreditDrawAction(credit_line_id="loc", day=9, amount_cents=1),
    ])
    assert len(res.rejected_actions) == 4
    assert closing(res, "a")[0] == 100_000  # nothing applied


def test_route_daily_cap_enforced(make_twin):
    res = simulate(make_twin(), actions=[
        TransferAction(route_id="a->b", day=0, amount_cents=60_000),
        TransferAction(route_id="a->b", day=0, amount_cents=60_000),  # exceeds $1,000/day
    ])
    assert len(res.rejected_actions) == 1


def test_negative_amount_impossible():
    with pytest.raises(ValidationError):
        TransferAction(route_id="a->b", day=0, amount_cents=-5)


def test_credit_draw_delay(make_twin):
    twin = make_twin(credit_lines=[{"id": "loc", "account": "b", "available_limit": 500,
                                    "draw_delay_days": 1, "fee_rate": 0.01}])
    res = simulate(twin, actions=[CreditDrawAction(credit_line_id="loc", day=0, amount_cents=50_000)])
    assert closing(res, "b") == [0, 50_000, 50_000, 50_000, 50_000]
    assert res.metrics.fees_cents == 500
