import pytest
from pydantic import ValidationError

from app.schemas import TwinOverrides, to_cents
from app.twin import apply_overrides, load_fixture


def test_to_cents_is_exact():
    assert to_cents(100000) == 10_000_000
    assert to_cents("0.1") == 10
    assert to_cents(19.99) == 1999
    with pytest.raises(ValueError):
        to_cents("1.005")


def test_fixture_loads_in_cents(golden):
    op = golden.account("operating")
    assert op.observed_balance_cents == 10_000_000
    assert op.minimum_buffer_cents == 5_000_000
    assert golden.routes[0].id == "reserve->operating"


def test_snapshot_hash_is_content_addressed(golden):
    assert load_fixture("golden").snapshot_id == golden.snapshot_id
    changed = apply_overrides(golden, TwinOverrides(account_buffers_cents={"operating": 1}))
    assert changed.snapshot_id != golden.snapshot_id


def test_overrides_do_not_mutate_input(golden):
    before = golden.model_dump()
    apply_overrides(golden, TwinOverrides(route_settlement_days={"reserve->operating": 2}))
    assert golden.model_dump() == before


def test_unknown_account_reference_rejected(make_twin):
    with pytest.raises(ValidationError):
        make_twin(obligations=[{"id": "x", "account": "nope", "amount": 1, "due_day": 0}])


def test_duplicate_ids_rejected(make_twin):
    with pytest.raises(ValidationError):
        make_twin(obligations=[{"id": "x", "account": "a", "amount": 1, "due_day": 0},
                               {"id": "x", "account": "a", "amount": 2, "due_day": 1}])


def test_bad_delay_distribution_rejected(make_twin):
    with pytest.raises(ValidationError):
        make_twin(expected_receipts=[{"id": "r", "account": "a", "amount": 1,
                                      "expected_day": 0, "delay_distribution": {0: 0.5}}])
