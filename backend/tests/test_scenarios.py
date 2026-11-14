from app.engine.models import Scenario
from app.engine.scenarios import evaluate_monte_carlo, sample_scenarios, scenario_probability


def test_sampling_is_reproducible(demo):
    a = sample_scenarios(demo, 50, seed=7)
    b = sample_scenarios(demo, 50, seed=7)
    assert [s.receipt_delays for s in a] == [s.receipt_delays for s in b]
    assert [s.receipt_delays for s in a] != [s.receipt_delays for s in sample_scenarios(demo, 50, seed=8)]


def test_correlated_receipts_delay_together(demo):
    procs = [r.id for r in demo.receipts if r.correlation_key == "card-processor"]
    for s in sample_scenarios(demo, 200, seed=1):
        delays = {s.receipt_delays.get(p, 0) for p in procs}
        assert len(delays) == 1


def test_sampled_delays_stay_in_support(demo):
    support = {r.id: set(r.delay_distribution) for r in demo.receipts}
    for s in sample_scenarios(demo, 200, seed=3):
        for rid, d in s.receipt_delays.items():
            assert d in support[rid]


def test_scenario_probability(demo):
    assert scenario_probability(demo, Scenario()) == 1.0
    p = scenario_probability(demo, Scenario(receipt_delays={"acme-inv-4471": 2}))
    assert abs(p - 0.25) < 1e-9  # P(delay >= 2) = 0.15 + 0.10


def test_monte_carlo_summary(demo):
    mc = evaluate_monte_carlo(demo, [], sample_scenarios(demo, 40, seed=0))
    assert mc.n == 40 and mc.breach_rate == 1.0  # no action -> payroll breach every time
