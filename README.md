# Buffer (repo: TreasuryTwin)

**Stripe Radar stops fraud before a payment goes through. Buffer stops a cash crunch before a payment goes through.**

Buffer is an API that apps (bill-pay, chat bots, checkouts, card authorizations) call *before* money moves. It simulates the proposed payment against a digital twin of every account, bill and expected receipt over the next 14 days, then returns **allow / warn / block** plus **verified fixes** (reschedule, fund from another account) that are replayed in exact cents.

On top of that API sits the **Buffer treasury agent**: a LangChain **deepagent** that runs the weekly payment run on its own. It watches every bank sync, plans with a live to-do list, delegates to three subagents, tries pay dates and funding options against every stress scenario, and proposes the plan with the best verified net benefit. Then it waits for a finance lead. Approval executes through Capital One's Nessie sandbox and reconciles. A rejection reason becomes a rule the agent follows from then on.

> Hackathon prototype. All data is synthetic. Not financial advice. No real money moves. Capital One Nessie is a sandbox, and writes to it are off by default.

## What's in the box

| Piece | Where |
|---|---|
| Product website | `http://localhost:5173/#/` |
| **Treasury agent (deepagents): plans the payment run, human approval, learns from rejections** | `#/agent` |
| Demo 1: bill pay checks (stored bills, sandbox fixes) | `#/bills` |
| Demo 2: purchase checks from a chat, a checkout and a card client sharing one cash twin | `#/purchases` |
| Public `/v1` API and interactive docs | `http://localhost:8000/docs` |

## Quickstart

Prerequisites: Python 3.11+, Node 18+.

**Windows (PowerShell)**

```powershell
Copy-Item .env.example .env          # set CASHGUARD_DEMO=1; Nessie / Anthropic keys are optional

python -m venv backend\.venv
backend\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt

cd backend
uvicorn app.main:app --reload        # API on http://127.0.0.1:8000
```

In a second terminal:

```powershell
cd frontend
npm install
npm run dev                          # http://localhost:5173
```

Vite proxies `/v1`, `/docs` and `/openapi.json` to `127.0.0.1:8000`.

macOS / Linux: `cp .env.example .env`, `python3 -m venv backend/.venv && source backend/.venv/bin/activate`, then the same `pip`, `uvicorn` and `npm` commands.

With `CASHGUARD_DEMO=1` the app boots with the synthetic fixture company **Acme Co** (`fixtures/cashguard_demo.json`): six accounts, 14 bills, 11 invoices, as-of 2026-11-20. Without it, it boots an older fixture company. Everything works without API keys; `ANTHROPIC_API_KEY` enables the treasury agent (`#/agent`), LLM explanations and the older deepagents stress mode; the rest of the app works without it.

**Optional: Nessie sandbox.** Set `NESSIE_API_KEY` and `NESSIE_CUSTOMER_ID` in `.env`, then mirror Acme Co into Nessie and sync it:

```powershell
backend\.venv\Scripts\python scripts\seed_nessie.py            # idempotent create / reconcile
backend\.venv\Scripts\python scripts\seed_nessie.py --reset    # delete what a previous run created
# then: POST /v1/connections/nessie/sync   (same curl shape as the fixture sync below)
```

`SANDBOX_WRITES_ENABLED=false` (default) keeps execution simulated; `true` also writes transfers to the Nessie sandbox.

**Tests:** `cd backend; python -m pytest -q` (181 tests, about 30 seconds; fully offline).

## API in 60 seconds

Every `/v1` route needs `Authorization: Bearer cg_test_demo_key` (the default `CASHGUARD_API_KEY`; one demo tenant).

```bash
AUTH="Authorization: Bearer cg_test_demo_key"
API=http://localhost:8000/v1

# 1. Load the demo company into the twin (reset=true wipes approvals and audit)
curl -X POST "$API/connections/fixture/sync?reset=true" -H "$AUTH"

# 2. Simulate paying a stored bill
curl -X POST $API/twin/simulations -H "$AUTH" -H "Content-Type: application/json" -d '{
  "proposed_action": {"bill_id": "BILL-NW-4471", "pay_date": "2026-11-20"}}'

# 3. Check a brand-new purchase (no bill needed). from_account is optional.
curl -X POST $API/twin/simulations -H "$AUTH" -H "Content-Type: application/json" -d '{
  "proposed_action": {"payee": "northwind", "amount_cents": 3000000,
                      "pay_date": "2026-11-20", "channel": "chat",
                      "description": "Rush order: steel brackets", "requested_by": "Priya (Ops)"}}'

# 4. Commit a checked purchase as a scheduled bill, optionally applying a fix (Idempotency-Key required)
curl -X POST $API/purchases/commit -H "$AUTH" -H "Content-Type: application/json" \
  -H "Idempotency-Key: order-123" -d '{"run_id": "<run_id>", "fix_id": "<fix id from step 3>"}'

# 5. Run the adaptive risk search in the background, then poll it
curl -X POST $API/twin/stress-tests -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"mode": "deterministic", "budget": 120}'
curl "$API/twin/stress-tests/latest" -H "$AUTH"

# 6. Fetch the grounded explanation for a decision
curl $API/runs/<run_id>/explanation -H "$AUTH"
```

`from_account` rule: an explicit value wins; otherwise the payee's most recent stored bill's source account; otherwise the operating account with the most cash above its buffer. The result is reported as `from_account` plus `from_account_source` (`explicit`, `vendor_default`, `primary_operating`, `bill`). `channel`, `description` and `requested_by` are echoed in provenance and the audit log and never change the decision.

Response to call 3 (abridged, real output on a fresh fixture sync):

```json
{
  "run_id": "run_...", "decision": "warn", "safe": false,
  "payee": "Northwind Supplies", "amount_cents": 3000000,
  "from_account": "operating", "from_account_source": "vendor_default",
  "breaks": [{"date": "2026-11-24", "account": "operating", "obligation": "Employees (payroll run)",
              "shortfall_cents": 2300000,
              "scenario": {"id": "shock-62", "label": "Acme Industrial pays 3d late", "plausibility": 0.2}}],
  "fixes": [{"id": "plan_resched_...", "kind": "reschedule", "summary": "Pay Northwind Supplies on Nov 25 instead of Nov 20",
             "pay_date": "2026-11-25", "cost_cents": 0, "verified": true, "held_out": {"breach_rate": 0.0, "paths": 100}}],
  "explanation": "...", "explanation_url": "/v1/runs/<run_id>/explanation",
  "latency_ms": 91
}
```

Other routes: `GET /v1/twin/state`, `POST /v1/twin/forecasts`, `POST /v1/twin/plans`, `GET /v1/plans/{fix_id}`, `POST /v1/plans/{fix_id}/approve`, `POST /v1/plans/{fix_id}/execute-sandbox`, `GET /v1/events`, `GET /v1/runs/{run_id}`, `GET /v1/connections`, `GET /v1/purchases/demo`. Full schemas are at `/docs`.

## The treasury agent

**Job:** schedule this week's approved bills. Every bill must be paid on a legal date, and every account must stay above its minimum buffer even under stress. Within that, the agent maximizes **net benefit = early-payment discounts captured − funding cost**. Vendors in the demo company offer real-world style terms such as Initech "2/2 net 30" (2% off if paid within 2 days).

**Who does what**

| | Language model (Claude via `langchain-anthropic`) | Deterministic engine |
|---|---|---|
| Decides which questions to ask, which schedules to try, which trade-off to propose | yes | |
| Computes balances, shortfalls, funding plans, costs, discounts | never | simulator + LP optimizer, exact cents |
| Can propose a plan | only a draft a tool has verified against every scenario | verifies |
| Moves money | never | gateway, after a finance lead approves |

**Structure (`backend/app/agent/ops.py`)**
- `create_deep_agent` with planning (`write_todos`, shown live in the UI) and three subagents:
  - `cash-analyst`: customer payment history and an adaptive shock search
  - `payables-planner`: tries schedules
  - `funding-planner`: transfers vs. credit
- Eight tools: `get_cash_position`, `list_open_bills`, `get_customer_history`, `predict_collections`, `run_risk_search`, `check_payment_run`, `read_preferences`, `submit_proposal`. Every call is recorded with the agent that made it, and that trace is the activity feed.
- **Human approval gate:** the run stops in `awaiting_approval`. Approving runs plain code: it builds one fix (bill schedules + transfers), records the approval, and executes through `actions/gateway.py`. With `SANDBOX_WRITES_ENABLED=true` it writes to Nessie, marking each bill scheduled with `PUT /bills/{id}` and booking each transfer as a withdrawal + deposit. It then re-syncs and reconciles. A newer proposal supersedes older ones, and a stale proposal is refused.
- **Learning from rejection:** the reason is stored as a preference. Keywords become rules the tools enforce, whatever the model says: naming an account protects it (`protect:reserve`), `credit` → `no_credit`, `transfer` → `no_transfers`. A revision run starts automatically, with the rejected plan and the reason in its prompt.
- **Learned payment-timing model** (`backend/app/ml/payment_timing.py`, agent tool `predict_collections`, `GET /v1/ml/payment-timing`): a scikit-learn gradient-boosted classifier for 0 / 1 / 2 / 3+ days late per open invoice. Features include customer base lateness, invoice size relative to the customer, month-end, quarter-end and terms. The demo company has only 7 paid invoices, so training adds a **synthetic** history generated from a documented rule. The model card reports holdout log-loss against the per-customer assumption the engine uses. The gain is small (about 1%) and is reported as such. It is advisory: the engine keeps its documented distributions, so decisions stay reproducible. Pointed at real deposit history, the same pipeline is what you would train.
- **Watcher:** after every fixture or Nessie sync (`CASHGUARD_AGENT_AUTO=1`, default on), Buffer checks for late receivables, new approved bills, expiring discounts and projected buffer breaches. It starts a run only for alerts no earlier run has answered.
- Without `ANTHROPIC_API_KEY`, or without the packages, a run fails with a clear reason. There is no silent fake. Tests use an injected fake runner.

**Agent API**: `POST /v1/agent/runs`, `GET /v1/agent/runs[/{id}|/latest]`, `POST /v1/agent/runs/{id}/approve`, `POST /v1/agent/runs/{id}/reject`, `GET|DELETE /v1/agent/preferences`, `POST /v1/agent/watch`, `POST /v1/agent/demo-events` (`late_payment` | `new_bill`).

## Demo scripts

Reset first with `POST /v1/connections/fixture/sync?reset=true` (or the Sync button in the UI).

**Demo 0, the treasury agent (`#/agent`), about 2 minutes**
1. Reset, open **Treasury agent**, click **Check for changes now**. Buffer reports the three expiring discounts and wakes up. The plan, the subagents and every tool call stream in (about 40 seconds).
2. Proposal: capture all three discounts ($3,250) by moving $198,000 from the strategic reserve to operating (fee $99), for a net **$3,151**. It is verified against every stress scenario. The $450k equipment bill is held back because it is still a draft.
3. Reject as Maya: *"Don't touch the strategic reserve this week."* The preference becomes the enforced rule `protect:reserve`, and a revision starts by itself.
4. Revision: pay everything on the due dates, net $0. The agent explains why, quoting its own checks: without the reserve, even the Initech discount alone leaves payroll short on Nov 24.
5. Approve. The bills are scheduled, and in Nessie mode with writes enabled the bills and transfers land in the sandbox and reconcile.
6. Click **Globex pays 4 days late**. The watcher wakes the agent with only that new alert.

**Demo 1, bill pay (`#/bills`)**
1. Sync. Six Acme Co accounts, bills and invoices load; the chart shows the 14-day outlook.
2. Pay the $5,000 office lease (`BILL-LEASE-11`): `allow`.
3. Pay Northwind $40,000 (`BILL-NW-4471`): `warn`, with the failing stress scenario named and verified fixes listed.
4. Approve a fix and execute it in the sandbox (simulated unless writes are enabled), then re-check: `allow`.
5. Pay the $450,000 equipment bill (`BILL-EQUIP-9`): `block`, no funding option keeps payroll and tax safe.
6. Risk lab: drag a customer's payment delay and watch the plan recompute from the backend.

**Demo 2, purchase checks (`#/purchases`)**: three clients share one twin, so each commitment changes the next answer.
1. **Card:** Dell, 12 laptops, $5,000 on Nov 20: `allow`. Commit it.
2. **Chat:** Northwind rush order, $30,000 on Nov 20: `warn`; the first fix is to reschedule to Nov 25. Commit with that fix.
3. **Checkout:** Haas CNC machine, $450,000 on Nov 20: `block`, no fix exists, and it cannot be committed.
4. **Card:** Midwest Manufacturing Expo booth, $25,000 on Nov 26. This was `allow` before step 2 was committed; now it is `warn` or `block` because of that commitment.

## How it works

```
 client (bill pay / chat / checkout / card)
        |  POST /v1/twin/simulations
        v
 decision layer --> digital twin (integer cents) --> day-by-day simulator
        |                    ^                              |
        |                    | connectors (fixture, Nessie) |
        v                    |                              v
 verified fixes <-- robust LP (SciPy HiGHS) <-- stress set <-- adaptive risk search
        |
        v
 execution gateway + reconciliation         async grounded explanation (Claude Haiku, template fallback)
```

- **Digital twin and simulator:** accounts, buffers, obligations, expected receipts (with customer delay distributions), internal routes with settlement lags and credit lines, all in integer cents. A day-by-day simulator replays any plan exactly.
- **Planner:** a scenario-robust linear program (SciPy HiGHS) picks the least-cost funding and timing, and runs on a persistent solver thread. It returns "infeasible" with the binding constraints rather than inventing amounts.
- **Evaluation:** Monte Carlo on held-out paths, with seeds disjoint from the ones used to discover stress scenarios.
- **Adaptive risk search:** deterministic search over shock combinations (late receipts, route outages, surprise outflows, credit unavailable). An optional deepagents orchestrator is available with `{"mode": "deepagent"}` and needs `ANTHROPIC_API_KEY`.
- **Decision layer:** `allow` (no breach in baseline or stress set), `warn` (a breach exists and a verified fix does too, or only a plan with residual risk), `block` (baseline breach and no feasible plan). The LLM never takes part in the decision.
- **Explanation:** generated asynchronously by Claude Haiku with every number checked against the run's results; falls back to a deterministic template (`CASHGUARD_EXPLAIN=0` forces this).
- **Execution:** the sandbox gateway applies approved fixes (simulated cash events, optionally Nessie transfers), bound to a snapshot and idempotent, then reconciles.

```
backend/app/
  api/v1.py          public /v1 routes (thin wrappers)
  services/          decision, fixes, purchases, stress, explain, impact, evaluation
  engine/            twin models, simulator, scenarios, LP optimizer, solver thread
  agent/             adaptive risk search, shock families, optional deepagents mode
  actions/           execution gateway, reconciliation
  connectors/        fixture, Nessie
  ingest/            normalize, sync, history
  workflows/         store, seed, AP/AR, forecast, policy
backend/tests/       pytest suite
fixtures/            cashguard_demo.json (Acme Co), older demo company
scripts/             seed_nessie.py, evaluate.py
frontend/src/
  site/              landing page          purchases/   Demo 2 clients
  tabs/              Demo 1 views          App.tsx, DemoApp.tsx   hash routing
```

## Evaluation

Reproduce: `backend\.venv\Scripts\python scripts\evaluate.py` (`--quick` for a smoke run; `--variants`, `--paths`, `--budget`, `--fixture`, `--out` also available). It writes `frontend/public/eval.json`, which the website's Evaluation section renders.

Protocol: 11 fixtures (the demo company plus 10 generated variants), 200 held-out Monte Carlo paths per fixture, search budget 120, fixed seeds. Held-out shocks: correlated customer delays, 5% route outage, 10% surprise outflow up to $20,000.

| Policy | What it does | Mean breach rate | Missed-obligation rate | Modeled cost (total) | Worst shortfall | Mean runtime |
|---|---|---|---|---|---|---|
| L | Pay everything on the latest legal date, no transfers | 36.1% | 0.23% | $0 | $66,700 | 147 ms |
| A | Pay on earliest date, top up accounts below buffer | 52.8% | 18.7% | $270.60 | $327,400 | 1.7 ms |
| B | LP on the expected timeline (no stress) | 36.9% | 18.7% | $295.25 | $327,700 | 9.5 ms |
| B+ | LP robust to the default stress set | 6.8% | 0.50% | $1,154.10 | $327,400 | 26.8 ms |
| C | B+ plus adaptive risk search discoveries | 6.4% | 0.50% | $1,204.43 | $327,400 | 744 ms |

Adaptive vs random search at equal budget (960 evaluations each): adaptive found 459 unique breaches versus 349 for random. Both found a breach on 8 of 11 fixtures; adaptive found an unfixable combination on 7 fixtures versus 8 for random, so the gain is breadth of findings, not every metric.

Honest reading:
- **L** has the lowest missed-obligation rate but still breaches the cash buffer about 36% of the time. Delaying everything is only valid when there is no early-pay discount or late penalty, and it cannot stop *new* spend before it is committed, which is Buffer's main use.
- **B+ and C** cut buffer breaches from about 37% to about 6.5% for a small modeled cost. C improves on B+ only modestly and is much slower per run.
- Breach rates are frequencies under synthetic distributions and a documented shock model, **not calibrated probabilities**.

## Hackathon tracks

| Track | How Buffer fits |
|---|---|
| **Business & Enterprise** | Treasury and AP tooling for small companies: the cash-safety API plus an agent that runs the payment run |
| **LangChain: best use of deepagents** | `create_deep_agent` with `write_todos` planning, three specialist subagents, tool-verified outputs, a human approval gate, and memory of rejections that changes later runs |
| **Amazon: practical AI** | It reasons, plans and takes multi-step actions, wakes itself on events, adapts to feedback (rejections become enforced preferences), and is reliable because every number comes from a verified engine |
| **Capital One: best use of Nessie** | Reads accounts, bills, deposits and withdrawals from Nessie. Approved runs write back (bill payment dates and scheduled state, transfers as withdrawal + deposit), then re-sync and reconcile |

## Where it fits

Spend tools such as Ramp or Brex control *who* may spend and *on what*: policies, limits, approvals. Buffer answers a different question: *can the company afford it, given every other account and obligation over the next weeks?* It is meant to be called by those tools, and by chat bots, checkouts and card authorizations, before they commit.

- **Complements policy:** a purchase can be fully approved and still collide with payroll next week.
- **Forward-looking:** limits tied to today's balance miss breaches that appear days later, when a late receivable meets a settlement lag.
- **Actionable:** it returns a verified fix, not only a chart or a score.

## Limitations

- Synthetic data only; the demo company is fictional and stress scenarios come from a documented model, not real settlement history.
- Single tenant with one static demo key; no per-customer auth or webhooks.
- Nessie balances are static: the sandbox ignores balance updates, amounts are truncated to whole dollars, and withdrawals and transfers cannot be deleted.
- 14-day default horizon (up to 30 per request); single currency, no FX modeling.
- Breach rates are not calibrated probabilities; the optimizer guarantees constraint satisfaction within the model, not the real world.
- Only fixture and Nessie connectors exist; Ramp, Stripe and QuickBooks are roadmap items.
- The LLM explanation is optional; it never decides or moves money.

## Credits and disclaimer

Built at HackPrinceton as a prototype. Not financial advice; no real money moves. Nessie is Capital One's public sandbox. No partnership with, or endorsement by, Capital One, Ramp, Stripe, QuickBooks, Brex or HackPrinceton is implied; names appear only to describe where Buffer could fit.
