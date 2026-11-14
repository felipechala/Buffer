# TreasuryTwin

Financial operations (receivables, payables, approvals, payment scheduling), with a predictive
digital twin built into every decision. Before a bill is approved or a payment is scheduled,
TreasuryTwin simulates what it does to every account, obligation and buffer, then proposes a
verified, least-cost funding plan.

**Sandbox prototype. All data is synthetic. Not financial advice. No real money moves.**

## Status

| Capability | State |
|---|---|
| Deterministic multi-account cash simulator (settlement lag, in-transit, credit delay, shocks) | Complete |
| Multi-day LP optimizer (SciPy HiGHS), scenario-robust, integer-cent replay, infeasibility reports | Complete |
| Monte Carlo receipt timing (correlated by customer), held-out seed separation | Complete |
| AR: customers, invoices (draft → issued → paid / overdue / void), simulated receipt posting | Complete |
| AP: vendors, bills (draft → pending → approved → scheduled → paid), policy, approvals, payment intents | Complete |
| Spend policy: >$10k requires a finance lead, category budgets, nondeferrable payroll/tax, no self-approval | Complete |
| `POST /api/bills/{id}/impact`: pay-date counterfactuals, nominal vs robust plans, held-out risk | Complete |
| Plan approval gate (finance lead, actionable only, rejects stale snapshots) | Complete |
| SQLite store with append-only audit trail | Complete |
| Adaptive risk-search agent + controller trace | Next |
| Nessie read adapter | Planned |
| Frontend (Overview, Receivables, Bill Pay, Decision Lab, Action Center) | Planned |
| Sandbox execution (feature-flagged, `SANDBOX_WRITES_ENABLED=false` by default) | Planned |

## Run

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
pytest -q
uvicorn app.main:app --reload   # http://127.0.0.1:8000/docs
```

The first start seeds the synthetic company from `fixtures/demo_company.json`.
`POST /api/demo/reset` re-seeds it. Mutating endpoints need an `X-User-Id` header (simulated
auth): `u_maya` (finance lead), `u_omar` (AP), `u_lee` (AR).

## Layout

```
backend/app/
  engine/       models, twin builder, simulator, optimizer, scenarios (pure, no I/O)
  workflows/    AR/AP state machines, policy, SQLite store, forecast bridge, seed
  services/     impact analysis (bill pay-date counterfactuals)
  agent/        adaptive risk search + controller          (next)
  adapters/     Nessie client                               (planned)
fixtures/
  demo_company.json            Northstar Software (synthetic) operating data
  engine/golden.yaml           v1 golden case: $120k transfer + $80k draw = $1,040
  engine/northstar_example.yaml  v2 worked example: $60k + $35k = $470
  engine/halcyon_stress.yaml   6-account stress fixture (paired-shock failure)
```

## Model conventions

- Three kinds of state are kept apart: **observed** (snapshot balances + posted cash events),
  **planned** (invoices, bills, payment intents) and **hypothetical** (scenarios, plans).
- Issued invoices are forecasts, never cash, until a posted cash event links them.
- Day 0 is the snapshot date. Event order per day: receipts & settlements → planned actions →
  unexpected outflows → obligations.
- Transfers debit the source on dispatch and credit the destination after `settlement_days`;
  in-flight cash is tracked separately.
- Route caps are per dispatch day; credit limits are cumulative over the horizon. Fees are a
  treasury cost, not deducted from balances.
- Robust plans must survive the *adverse* scenario (every receipt at its (1 − tolerance) delay
  quantile at the same time). They are then frozen and scored on held-out Monte Carlo seeds.
  Breach rates are frequencies under synthetic distributions, not calibrated probabilities.
