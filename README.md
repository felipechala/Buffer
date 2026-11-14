# TreasuryTwin

A sandbox-backed financial digital twin: it projects multi-account cash positions, searches for
plausible shortfalls, and computes constrained, verified funding/transfer plans.
**All data is synthetic. Not financial advice. No real money moves.**

## Status

| Capability | State |
|---|---|
| Typed data model (integer cents, immutable hashed snapshots) | Complete |
| Deterministic multi-account simulator (transfer queue, credit delay, shocks) | Complete |
| Multi-day LP planner (SciPy HiGHS) with infeasibility reports | Complete |
| FastAPI: sync (fixture), scenarios, optimize, overrides | Complete |
| Risk-search agent, agent loop, held-out verification | Next |
| Nessie read adapter | Planned |
| Frontend | Planned |
| Sandbox execution + approval gate | Planned (feature-flagged) |

## Run

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
pytest -q
uvicorn app.main:app --reload   # http://127.0.0.1:8000/docs
```

## Model conventions

- Day 0 is the snapshot date; time is in daily buckets.
- Event order per day: receipts & settlements → planned actions → unexpected outflows → obligations.
  The closing balance is therefore the binding point for buffer checks.
- Transfers debit the source on dispatch and credit the destination after `settlement_days`;
  in-flight cash is tracked separately so internal transfers never create cash.
- Route caps are per dispatch day; credit limits are cumulative over the horizon.
- Fees are a treasury cost reported in metrics, not deducted from balances.

## Fixtures

- `fixtures/golden.yaml`: the guide's two-account example. The planner must return a $120k transfer
  plus an $80k draw at a $1,040 cost, and become infeasible with 2-day settlement.
- `fixtures/demo_company.yaml`: a fictional six-account company. Each shock on its own is covered,
  but the paired shock (Acme invoice 2 days late plus the reserve route unavailable) is infeasible.
