import os

# API tests sync the fixture constantly; the background risk search is exercised explicitly in test_search.py.
os.environ.setdefault("CASHGUARD_AUTO_STRESS", "0")
# The suite must never hit the network (explainer / deepagents).
os.environ["CASHGUARD_EXPLAIN"] = "0"

import pytest

from app.engine.twin import load_fixture, twin_from_fixture_dict


@pytest.fixture
def golden():
    return load_fixture("golden")


@pytest.fixture
def demo():
    return load_fixture("halcyon_stress")


@pytest.fixture
def make_twin():
    """Build a small inline twin; dollars in, cents inside."""
    def _make(**overrides):
        raw = {
            "company": {"name": "Inline Co", "horizon_days": 5},
            "accounts": [
                {"id": "a", "observed_balance": 1000, "min_buffer": 0},
                {"id": "b", "observed_balance": 0, "min_buffer": 0},
            ],
            "transfer_routes": [
                {"id": "a->b", "from": "a", "to": "b", "settlement_days": 0,
                 "max_amount": 1000, "fee_rate": 0.0},
            ],
        }
        raw.update(overrides)
        return twin_from_fixture_dict(raw)
    return _make
