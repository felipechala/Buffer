# CashGuard — combined build plan

> **Stripe Radar stops fraud before a payment goes through. CashGuard stops a cash crunch before a payment goes through.**

CashGuard is a developer API that payroll, invoicing, bill-pay and card apps call **before money moves**. It simulates the payment against the company's whole multi-account cash future, then returns one answer:

1. **Safe?** Will this payment push any account below its buffer in the next 14 days, including under plausible shocks?
2. **What breaks, and when?** For example, "Payroll on Nov 24 is short $18,400 if Acme's invoice is 2 days late."
3. **The cheapest fix.** Move the payment date, transfer between accounts, or draw on credit. The fix is chosen by an optimizer and re-checked by a verifier. If no fix exists, CashGuard says so and explains why.

It is built on the **TreasuryTwin engine** (a deterministic simulator plus a linear-program planner, both already in this repo). **Capital One Nessie** is the banking layer: it is the source of balances, bills, payees and payment history, and it is where approved fixes run in sandbox.

**The website is a demo, not the product.** It shows a made-up third-party bill-pay app calling the API, next to a live inspector that shows every request and response. The API is what we're selling.

*Synthetic data only. Not financial advice. No real money moves. Nessie is a sandbox.*

---

## 0. Where this plan comes from

This plan merges two documents:

- **The repo's TreasuryTwin v2 guide** (`TreasuryTwin_Financial_Operations_Digital_Twin_Build_Guide_v2.pdf`). It proposed a full finance-ops app (AR/AP, approvals, budgets) with the twin built into each workflow.
- **The CashGuard pitch.** It proposed an API-first product with a single `/v1/check` call.

| From the CashGuard pitch | From the TreasuryTwin v2 guide | Dropped |
|---|---|---|
| API-first product with the `allow / warn / block` contract | Bill Pay screen, reused as the demo client | AR invoice CRUD, budgets, spend policies |
| Multiple ranked fixes | Non-LLM adaptive shock search as the core of the agent | Full AP/AR state machines, roles, SQLite workflows |
| deepagents orchestrator + grounded LLM explainer | Verifier on held-out scenarios | Receivables, Decision Lab and Action Center pages |
| `/v1/stress`, `/v1/forecast`, `/v1/decisions` | Benchmark of adaptive vs. random search | Multi-bill mixed-integer scheduling |
| Competitive positioning, judge Q&A, demo script | Nessie failure handling, approval gate, idempotency, reconciliation | |

### What already exists in this repo

| Exists | Not built yet |
|---|---|
| Integer-cent data model, immutable hashed snapshots (`schemas.py`, `twin.py`) | `/v1/check` and the decision logic |
| Day-by-day simulator with transfer queue, credit delay and shocks (`simulator.py`) | Payment rescheduling. **The LP has no date-shift variable**, so we enumerate dates instead (§4.3) |
| Multi-day HiGHS LP with infeasibility reports (`planner.py`) | Verifier, risk-search agent, explainer |
| FastAPI: `/v1/twin/sync`, `/v1/scenarios/run`, `/v1/plans/optimize`, overrides | Nessie adapter, seeding script, write path |
| `fixtures/demo_company.yaml`, `fixtures/golden.yaml`, pytest suite | A demo fixture that matches the script (§7). The current one is Halcyon, 7 days, and has the reserve→payroll route disabled |
| | Frontend |

Note: `ExpectedReceipt.delay_distribution` exists in the schema, but the engine doesn't use it yet. The verifier will use it to sample held-out scenarios.

---

## 1. The problem

Companies with several bank accounts (operating, payroll, reserve, credit line) approve payments one at a time. Cash problems come from **how those payments combine**:

- A $40k vendor payment looks fine today. But a customer invoice arrives late, a reserve transfer takes time to settle, and payroll hits Thursday. Together, payroll bounces.
- The people approving payments (AP clerks, founders, ops managers) see today's balance, not the next 14 days.
- Treasury suites that model this (Kyriba, HighRadius, Tesorio) are enterprise dashboards. They don't run as an automatic check inside the tools where payments happen.
- SMBs rely on spreadsheets and a manual rule of thumb: "release a payment if the post-payment balance stays above the floor."

The results are overdrafts, bounced payroll, emergency credit draws and late fees, all of which could have been predicted days in advance.

---

## 2. The API (the product)

### 2.1 Core call

```http
POST /v1/check
Authorization: Bearer cg_test_demo_key
Content-Type: application/json

{
  "amount_cents": 4000000,
  "from_account": "operating",
  "payee": "Northwind Supplies",
  "date": "2026-11-20",
  "latest_date": "2026-11-27",
  "priority": "normal"
}
```

```json
{
  "id": "dec_01HZX4...",
  "decision": "warn",
  "safe": false,
  "breaks": [
    {
      "date": "2026-11-24",
      "account": "payroll",
      "obligation": "Biweekly payroll",
      "shortfall_cents": 1840000,
      "scenario": { "id": "acme_late_2d", "label": "Acme invoice 4471 arrives 2 days late", "plausibility": 0.6 }
    }
  ],
  "fixes": [
    {
      "id": "fix_1",
      "summary": "Pay Northwind on Nov 25 instead of Nov 20",
      "cost_cents": 0,
      "delay_days": 5,
      "actions": [{ "type": "reschedule", "date": "2026-11-25" }],
      "verified": true
    },
    {
      "id": "fix_2",
      "summary": "Pay on Nov 20; transfer $18.4k reserve → payroll on Nov 21",
      "cost_cents": 920,
      "actions": [{ "type": "transfer", "route": "reserve-to-payroll", "from": "reserve", "to": "payroll", "amount_cents": 1840000, "dispatch": "2026-11-21" }],
      "verified": true
    },
    {
      "id": "fix_3",
      "summary": "Pay on Nov 20; draw $18.4k on the revolver",
      "cost_cents": 23000,
      "actions": [{ "type": "credit_draw", "line": "revolver", "amount_cents": 1840000, "date": "2026-11-23" }],
      "verified": true
    }
  ],
  "explanation": "Paying $40,000 on Nov 20 is safe if Acme pays on time, but if Acme invoice 4471 arrives 2 days late, payroll on Nov 24 is short $18,400. Moving the payment to Nov 25 avoids this at no cost.",
  "stress": { "scenarios_checked": 24, "source": "cached_risk_search" },
  "snapshot": { "hash": "sha256:9f2c…", "source": "nessie", "synced_at": "2026-11-20T14:02:11Z" },
  "latency_ms": 112
}
```

The numbers above are examples. The real values come from the demo fixture (§7); the golden tests pin them, not this document.

### 2.2 Decision levels

| Decision | Rule | Default behavior in the calling app |
|---|---|---|
| `allow` | No buffer breach in the baseline **or** in any stress scenario in the cached set | The payment proceeds |
| `warn` | Breaches in the baseline or under a stress scenario, **and at least one verified fix exists** | Show the reason and fixes; a person chooses |
| `block` | Breaches in the baseline **and no fix exists** within the constraints | The payment is held; an infeasibility report is attached |

A breach that only shows up under a stress scenario and has no fix is still `warn`, with `fixes: []` and the infeasibility report attached. We don't block on a hypothetical alone.

### 2.3 Endpoints

| Endpoint | Purpose | Notes |
|---|---|---|
| `POST /v1/check` | Core check: decision, breaks, fixes, explanation | Target under 300 ms using the cached stress set |
| `POST /v1/check/batch` | Check a whole payment run; return the largest safe subset for today | Stretch: greedy by priority, re-checked |
| `POST /v1/fixes/{id}/apply` | Run a fix in the Nessie sandbox | Needs `approved_by` and an `Idempotency-Key` header; gated by `SANDBOX_WRITES_ENABLED` |
| `GET /v1/forecast` | 14-day balances per account: baseline plus the worst shock found | Feeds the chart |
| `POST /v1/stress` | Run the risk-search agent; return ranked dangerous shock combinations | Async-friendly; results cached per snapshot |
| `POST /v1/sync` | Pull a new snapshot from Nessie (`source: "nessie"`) or from a fixture | Re-runs the stress search in the background |
| `GET /v1/decisions/{id}` | Audit record: snapshot hash, inputs, decision, fixes, approver, Nessie IDs | |
| `GET /v1/decisions` | Decision log | Feeds the demo log panel |

The existing `/v1/twin/*`, `/v1/scenarios/run` and `/v1/plans/optimize` routes stay as internal engine endpoints.

### 2.4 API conventions (Stripe-style)

- **Auth:** `Authorization: Bearer cg_test_...`. One static test key from `.env` is enough for the demo.
- **Money:** always integer cents. **Dates:** ISO `YYYY-MM-DD`, converted to day indexes relative to the snapshot's `as_of`.
- **Errors:** `{ "error": { "type": "invalid_request" | "snapshot_stale" | "nessie_unavailable" | "solver_failed", "message": "...", "param": "..." } }`
- **Idempotency:** writes take an `Idempotency-Key` header. A repeated key returns the stored result and never makes a second Nessie write.
- **Never return an actionable fix if the solver or verifier failed.** Return the error instead.
- **OpenAPI docs** are at `/docs` (FastAPI). These are the developer docs we show judges.

---

## 3. Architecture

```
 Demo client: "Acme Payroll & Bills"            API inspector (same page)
          │  POST /v1/check  ─────────────────────► shows request / response / latency
          ▼
 ┌───────────────────────────────┐
 │ 1. Snapshot loader            │  Nessie (accounts, bills, deposits, merchants)
 │                               │  + treasury config overlay (buffers, routes, credit)
 └──────────────┬────────────────┘
                ▼
 ┌───────────────────────────────┐
 │ 2. Simulator (exists)         │  payment inserted as an obligation; baseline + cached shocks
 └──────────────┬────────────────┘
                ▼
 ┌───────────────────────────────┐
 │ 3. Risk search (cached)       │  deterministic adaptive search, orchestrated by a deepagent
 └──────────────┬────────────────┘
                ▼
 ┌───────────────────────────────┐
 │ 4. Fix generator              │  date enumeration + LP (HiGHS) + forbid-one-action re-solves
 └──────────────┬────────────────┘
                ▼
 ┌───────────────────────────────┐
 │ 5. Verifier                   │  integer-cent replay: baseline + stress set + held-out samples
 └──────────────┬────────────────┘
                ▼
 ┌───────────────────────────────┐
 │ 6. Explainer (LLM, grounded)  │  1–2 sentences; every number checked against the payload
 └──────────────┬────────────────┘
                ▼
     allow / warn / block + fixes ──► audit log
                │
      (human approves a fix)
                ▼
 ┌───────────────────────────────┐
 │ 7. Executor (Nessie sandbox)  │  transfer / bill reschedule → re-sync → reconcile
 └───────────────────────────────┘
```

**Rule:** the AI never decides and never moves money. The agent proposes scenarios and writes explanations. The optimizer produces the plans. The verifier checks them. A person approves. Only the executor writes to Nessie, and only in sandbox.

---

## 4. How `/v1/check` works

### 4.1 Snapshot

An immutable, hashed picture of company cash at day 0 (already built: `TwinState` + `snapshot_hash`). It is built from two sources:

- **Nessie:** observed balances, scheduled bills, expected deposits, payees, and past deposits used for payment history. See §5.
- **Treasury config** (`fixtures/cashguard_treasury.yaml`): things Nessie can't represent. That means minimum buffers, transfer routes (settlement days, daily caps, fees, allowed/blocked), credit facility terms, obligation priority and deferrability, and the shock library's bounds.

The snapshot response records `source` (`nessie` or `fixture`) and `synced_at`. When Nessie isn't available, we fall back to the fixture and label it clearly; we never invent data.

### 4.2 Decision logic

```
1. Insert the payment as an obligation on its date (priority from the request).
2. Simulate the baseline.
3. Simulate each scenario in the cached stress set (top-K from the risk search,
   plus single shocks from the library with plausibility ≥ threshold).
4. If nothing breaches → allow.
5. Otherwise, the target scenario is the baseline if it breaches, else the most
   plausible breaching shock. Generate fixes for it (4.3).
6. Verify every fix (4.4). Drop any that fail.
7. If verified fixes remain → warn.
   If none, and the baseline breaches → block with an infeasibility report.
   If none, and only a stress scenario breaches → warn with fixes = [] and the report.
8. Explain (4.5), log the decision, return.
```

The stress set is computed by `/v1/sync` and `/v1/stress` and cached per snapshot hash, so `/v1/check` never waits on the agent.

### 4.3 Fix generator

| Fix family | Method |
|---|---|
| **Reschedule** | Try each date from `date + 1` to `latest_date`. Simulate the payment on that date under the target scenario. Pick the earliest date that is safe. Cost = 0 fee plus a configurable delay penalty used only for ranking. *(This uses enumeration because the LP has no date variable. Enumeration is cheap and explainable.)* |
| **Cheapest overall** | Existing `optimize_plan` under the target scenario with the payment on its requested date: transfers plus credit draws at minimum cost |
| **No credit** | Re-solve with credit lines disabled (`Scenario.disabled_credit_lines`) |
| **No transfers** | Re-solve with routes disabled (credit only) |

Remove duplicates, rank by `cost_cents`, return the top 3. If the LP is infeasible, return its existing `infeasibility` report, which names the binding constraint. For example: "Route reserve-draw settles in 1 day; earliest arrival is after the day-4 deadline."

Stretch: a robust LP that stacks the balance constraints for several scenarios, so one action set covers all of them.

### 4.4 Verifier

The verifier runs each fix back through `simulate()` in exact integer cents, independently of the LP:

- the baseline,
- every scenario in the cached stress set,
- **held-out samples**: N = 50 scenarios drawn from each receipt's `delay_distribution`, using a seed set kept separate from the risk search's seeds.

A fix is `verified: true` only if the baseline and the stress set pass. The response reports the held-out pass rate (`"held_out": {"passed": 48, "of": 50}`) as information; it doesn't block the fix. This catches cases where the LP and the simulator disagree, such as rounding or event order.

### 4.5 Explainer

An LLM call (`CASHGUARD_EXPLAIN_MODEL`, default `claude-haiku-4-5-20251001` for speed) receives **only** the structured result: breaks, scenario label, fixes, binding constraints. It returns 1–2 sentences.

**Grounding check:** pull every money amount and date out of the text, and require each one to match a value in the payload. Any mismatch, a timeout, or a missing API key falls back to a template sentence. The demo never depends on the LLM.

---

## 5. Nessie integration

Nessie is the banking substrate. CashGuard **reads** company state from it, **learns** payment history from it, and **writes** approved fixes to it. All of this happens in the sandbox.

> Confirm exact routes and field names against `nessieisreal.com/docs` with our key in hour 0. Save one sanitized real response per endpoint to `fixtures/nessie/` for contract tests.

### 5.1 Setup

- `.env`: `NESSIE_API_KEY`, `NESSIE_BASE_URL=http://api.nessieisreal.com`, `SANDBOX_WRITES_ENABLED=false` by default.
- The key stays in the backend only. Never put it in the frontend bundle, logs or git.
- Auth is the `?key=` query parameter on every call.

### 5.2 Seeding the demo company (`scripts/seed_nessie.py`)

Creates the demo company in Nessie from `fixtures/cashguard_demo.yaml`. It is safe to re-run: it matches on nickname and writes `fixtures/nessie_ids.json` (gitignored).

| What | Nessie call | Demo content |
|---|---|---|
| Company | `POST /customers` | "Acme Co (synthetic)" |
| Accounts | `POST /customers/{id}/accounts` (`type`: Checking / Savings) | operating, payroll, tax, reserve, settlement, intl_clearing (6 accounts) |
| Payees | `POST /merchants` | Northwind Supplies, Initech Components, landlord, insurer, … |
| Scheduled outflows | `POST /accounts/{id}/bills` (`status: pending/recurring`, `payment_date`, `payment_amount`, `payee`) | Payroll, rent, supplier bills within the 14-day horizon |
| Expected inflows | `POST /accounts/{id}/deposits` (`status: pending`, future `transaction_date`, `description: "INV-4471 Acme Industrial"`) | Customer invoices |
| **Payment history** | `POST /accounts/{id}/deposits` (`status: completed`, past dates, `description: "INV-4302 Acme Industrial due 2026-10-01"`) | Acme paid 3 of its last 5 invoices late; Globex is always on time |
| Reset | `DELETE /data?type=...` (verify) | Clean slate before the demo |

If Nessie rejects future-dated pending deposits, expected receipts move into the treasury config overlay. Everything else still comes from Nessie.

### 5.3 Read path (`backend/app/adapters/nessie.py` + `nessie_mapper.py`)

| Nessie | Becomes | Rule |
|---|---|---|
| `GET /customers/{id}/accounts` → `balance` (dollars) | `Account.observed_balance_cents` | Convert to integer cents at the boundary. Match accounts to internal IDs by nickname |
| `GET /accounts/{id}/bills`, status pending/recurring, inside the horizon | `Obligation` | Payee → counterparty. Priority and deferrability come from the config overlay |
| `GET /accounts/{id}/deposits`, status pending, future date | `ExpectedReceipt` | **Forecast, not posted cash** |
| `GET /accounts/{id}/deposits`, status completed, has `due` in its description | Payment history | Feeds the reliability scores (§5.5) |
| `GET /merchants` | Payee directory | Used to validate `payee` in `/v1/check` |
| `GET /accounts/{id}/loans` | Shown as context only | **Credit availability is never inferred from Nessie.** It comes from the config overlay |

The mapper is a pure function (Nessie JSON → `TwinState`) and is contract-tested against the recorded fixtures.

### 5.4 Write path: applying an approved fix

`POST /v1/fixes/{id}/apply` with `{ "approved_by": "demo-cfo" }` and an `Idempotency-Key` header:

| Fix action | Nessie write | Label shown in the UI |
|---|---|---|
| `transfer` | `POST /accounts/{from}/transfers` (`medium: balance`, `payee_id: to`, `amount`, `transaction_date`, `description: "CashGuard fix_2 / dec_…"`) | "Executed in Nessie sandbox" + Nessie transfer ID |
| `reschedule` | `POST /accounts/{id}/bills` with the new `payment_date` (or `PUT /bills/{id}` if the bill already exists) | "Scheduled in Nessie sandbox" + bill ID |
| `credit_draw` | Not supported by Nessie. Recorded as a labeled deposit `"Revolver draw (simulated)"`, or kept internal only | "Simulated credit draw" |

After any write:

1. Re-sync from Nessie and build a new snapshot.
2. **Reconcile:** compare our expected balances with what Nessie now reports. Show "Reconciled: $0 unexplained difference", or flag the gap. Settlement lag is modeled by our engine, not by Nessie, so in-flight amounts are explained by the route's `settlement_days`.
3. Re-run `/v1/check` on the original payment; it should come back green.

### 5.5 Customer reliability from Nessie history

For each customer, `reliability = on-time share` and `mean_delay_days` are computed from completed deposits (actual date vs. the `due` date in the description). The risk search uses these numbers to weight shock plausibility, which is why "Acme late by 2 days" ranks above "Globex late". Honesty rule: this is computed from **seeded synthetic history**. We don't claim the model "learned from Nessie".

### 5.6 Failure handling

| Condition | Behavior |
|---|---|
| 401/403 or bad key | Integration warning banner; use the last snapshot or the fixture; never invent data |
| 429 / 5xx / timeout | Bounded read retries; show the stale `synced_at`; fall back to the fixture explicitly |
| Unknown response schema | Mapper fails with a tagged error and saves a redacted sample |
| Write times out | Status becomes `uncertain`; re-read before any retry; never retry a write blindly |
| Writes disabled | Apply returns a simulated execution, clearly labeled |

---

## 6. Risk-search agent

There are two layers. The deterministic core does the finding; the LLM layer coordinates the search and explains it.

### 6.1 Deterministic core (`backend/app/agent/search.py`, no LLM)

- **Shock library** (bounds from the treasury config):

  | Shock | Bounds |
  |---|---|
  | Late receipt | 0–5 days, per customer |
  | Partial receipt | 70–100% |
  | Unexpected outflow | $0–$20k |
  | Route outage | One route |
  | Credit draw delayed or unavailable | — |

- **Score** = breach severity (shortfall × days) × plausibility (from §5.5 reliability) + a novelty bonus.
- **Best-first mutational search:** start from single shocks. Expand the highest-scoring ones by mutating a parameter or adding a second shock. Keep breaching combinations. Prefer the **smallest** shock that causes a breach, because it's easier to explain.
- **Output:** a ranked "danger list". Each entry has the shocks, first breach, shortfall, plausibility, and whether a fix exists (LP feasibility).
- **The key finding:** compound shocks. A late Acme invoice alone is survivable, the reserve route being down alone is survivable, and both together are infeasible.

### 6.2 deepagents layer (`backend/app/agent/deep.py`)

- LangChain deepagents orchestrator (`CASHGUARD_AGENT_MODEL`, default `claude-sonnet-5-5`).
- **Tools:** `search_shocks(snapshot, budget, focus)`, `simulate(snapshot, shocks)`, `optimize(snapshot, shocks)`, `list_shocks()`, `get_history(customer)`.
- **Subagents:** one per shock family (receivables, routes, outflows). Each looks at history and config and chooses where `search_shocks` should focus.
- **Output:** the same danger-list schema, plus a short trace of what it tried and why. The trace is shown in the Stress view.
- **Fallback:** if the LLM is unavailable, `/v1/stress` runs the deterministic core alone. The demo still works.

### 6.3 Benchmark (`scripts/benchmark_search.py`)

This is cheap to build and is our main evidence for the AI tracks.

- Compare adaptive search with random search **at the same simulation budget** on the demo fixture plus about 10 generated fixtures with fixed seeds.
- **Metrics:**
  - simulations needed to find the first compound breach,
  - recall of labeled failure cases,
  - runtime.
- Report the raw numbers in the README and as one chart on the Stress view. Claim only what we measured.

---

## 7. Demo fixture (`fixtures/cashguard_demo.yaml`)

This is new. It replaces Halcyon for the demo and is seeded into Nessie by §5.2. It is tuned until these **golden tests** pass:

| Test | Expected |
|---|---|
| `as_of` | 2026-11-20 (day 0); horizon 14 days |
| $5,000 bill from operating, Nov 20 | `allow` |
| $40,000 to Northwind, Nov 20 | `warn`. Baseline is safe. Under "Acme invoice 4471 two days late", **payroll on Nov 24** breaches. Fixes are returned in order reschedule → reserve transfer → credit draw, all `verified: true` |
| Apply the reserve transfer, re-check the $40k | `allow` |
| Stress: Acme 2 days late, alone | Survivable |
| Stress: reserve route down, alone | Survivable |
| Stress: Acme late **and** reserve route down, with the $40k paid | Infeasible. The report names the binding constraint |
| $250,000 bill, Nov 20 | `block` (baseline breach, no feasible fix) |
| Cent replay of every returned fix | No buffer breached |

Unlike the current Halcyon fixture, the **reserve→payroll route must be allowed** (for example, 1-day settlement) so that fix 2 is a real option. The exact shortfall (≈$18,400) comes from tuning, and the script quotes whatever the tests pin.

---

## 8. The website: a demo, not the product

**Stack:** Vite + React + TypeScript + Tailwind + Recharts. One page, three regions. Every control calls the real API; nothing is decorative.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ CashGuard demo     Data: ● Nessie sandbox · synced 12s ago   [Sync from Nessie]│
├───────────────────────────────────┬──────────────────────────────────────────┤
│  ACME PAYROLL & BILLS (fake client)│  API INSPECTOR                           │
│  (styled as a separate app)        │  POST /v1/check            112 ms  200   │
│                                    │  ▸ request  { amount_cents: 4000000 … }  │
│  Northwind Supplies   $40,000 [Pay]│  ▸ response { decision: "warn", … }      │
│  Office lease          $5,000 [Pay]│  [Copy as curl] [Python] [JS]            │
│  Big equipment       $250,000 [Pay]│                                          │
│                                    │  History: check · check · apply · sync   │
│  ┌ ⚠ Payment needs review ───────┐ │                                          │
│  │ Payroll Nov 24 short $18,400  │ │                                          │
│  │ if Acme pays 2 days late.     │ │                                          │
│  │ ① Pay Nov 25 instead   $0     │ │                                          │
│  │ ② Transfer reserve     $9.20  │ │                                          │
│  │ ③ Draw revolver        $230   │ │                                          │
│  │ [Approve & apply ②]           │ │                                          │
│  └───────────────────────────────┘ │                                          │
├───────────────────────────────────┴──────────────────────────────────────────┤
│ Tabs:  [Forecast]  [Stress / danger list]  [Decision log]                    │
│  Forecast: 14-day balance per account, buffer lines, baseline vs worst shock │
│  Stress: ranked shock combos, infeasibility report, agent trace, benchmark   │
│  Log: decision id, snapshot hash, decision, approver, Nessie transfer id     │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **Left, the fake client.** It's deliberately styled as someone else's app, with a different brand color and font. This makes the point that any app can add CashGuard. Clicking Pay calls `/v1/check` and shows a green, yellow or red card. "Approve & apply" calls `/v1/fixes/{id}/apply`, shows the returned Nessie transfer ID, re-syncs, re-checks, and the card turns green.
- **Right, the API inspector.** This is the hero of the page. It shows every request and response live, with latency and copy-as-curl/Python/JS snippets. Judges should see that the product is the API call.
- **Bottom tabs.**
  - **Forecast:** `/v1/forecast` chart.
  - **Stress:** `/v1/stress` danger list, the infeasibility report, the agent trace, and the benchmark chart.
  - **Log:** `/v1/decisions` audit trail.
- **Data badge.** Always shows `Nessie sandbox` or `Fixture mode` and the sync time. Observed balances, forecasts and executed actions are labeled differently.
- **Developer docs** are FastAPI's `/docs` plus a README quickstart. We don't build a separate docs site.
- **Stretch:** a `<cashguard-check>` web component that wraps the left card so it can be dropped into any app.

---

## 9. Repo layout (additions)

```
backend/app/
  api/v1.py                 # /v1/check, fixes, forecast, stress, sync, decisions
  cashguard/check.py        # decision logic (§4.2)
  cashguard/fixes.py        # reschedule enumeration + LP variants (§4.3)
  cashguard/verifier.py     # cent replay + held-out sampling (§4.4)
  cashguard/explain.py      # LLM explainer + grounding check (§4.5)
  cashguard/decisions.py    # in-memory audit log (JSON file dump for the demo)
  adapters/nessie.py        # httpx client, retries, error mapping
  adapters/nessie_mapper.py # Nessie JSON + treasury config → TwinState
  agent/shocks.py           # shock library + bounds
  agent/search.py           # deterministic adaptive search
  agent/deep.py             # deepagents orchestrator + subagents
  executor.py               # sandbox writes, idempotency, reconcile
backend/tests/
  test_check_golden.py      # §7 golden tests
  test_nessie_mapper.py     # contract tests on fixtures/nessie/*.json
  test_verifier.py, test_search.py, test_executor.py
fixtures/
  cashguard_demo.yaml       # demo company (§7)
  cashguard_treasury.yaml   # buffers, routes, credit, shock bounds, nickname map
  nessie/                   # recorded, sanitized Nessie responses
scripts/
  seed_nessie.py, benchmark_search.py
frontend/                   # Vite + React demo (§8)
```

Add to `.env.example`: `CASHGUARD_API_KEY`, `SANDBOX_WRITES_ENABLED=false`, `ANTHROPIC_API_KEY`, `CASHGUARD_AGENT_MODEL`, `CASHGUARD_EXPLAIN_MODEL`, `SIMULATION_DEFAULT_SEED=42`.

Add to `requirements.txt`: `deepagents`, `langchain-anthropic`, `anthropic`.

---

## 10. Build plan (~19 hours to the 8:00 AM deadline)

| Block | Hours | Backend | Integrations (Nessie) | AI | Frontend |
|---|---|---|---|---|---|
| 1 | 0–3 | `cashguard_demo.yaml` + golden tests; `/v1/check` with baseline + reschedule + LP fix | Confirm Nessie routes with our key; record fixtures; `seed_nessie.py` | Shock library + deterministic search skeleton | Vite app; fake client + API inspector layout; Pay → `/v1/check` |
| 2 | 3–7 | Fix variants; verifier; decision log; `/v1/forecast` | Read adapter + mapper + contract tests; `/v1/sync source=nessie` | Best-first search + plausibility from history; cached stress set wired into check | Result card, fixes, forecast chart, data badge |
| 3 | 7–10 | `/v1/fixes/{id}/apply` + idempotency + approval gate | Write path (transfers, bills) + re-sync + reconcile | deepagents layer + trace; explainer + grounding check; `/v1/stress` | Apply flow (Nessie ID, re-check to green); Stress tab; Log tab |
| — | 10–14 | Sleep in shifts. **Freeze features at hour 14.** | | | |
| 4 | 14–17 | Latency, error envelopes, polish | Re-seed and dry-run on a clean Nessie | Benchmark script + chart | Polish; copy-as-curl; stretch: batch / widget |
| 5 | 17–18.5 | README (quickstart, curl), Devpost, record the 2-minute video, push | | | |
| — | by 8:00 AM | Submit, with Discord usernames | | | |

**Cut list** (cut from the top first):
1. Widget
2. Card-auth mode
3. Batch endpoint
4. deepagents layer (keep the deterministic search)
5. Benchmark chart
6. Held-out sampling in the verifier (keep stress-set replay)
7. Nessie writes (keep reads; label execution as simulated)

**Never cut:** `/v1/check` with verified fixes, the API inspector, the compound-shock finding, Nessie reads.

---

## 11. Demo script (2 minutes)

| Time | Beat |
|---|---|
| 0:00–0:15 | **Problem.** "Small businesses don't go under from one bad payment. They go under from two normal things happening the same week." |
| 0:15–0:30 | **Setup.** Click *Sync from Nessie*. The badge turns green; six Acme Co accounts, bills and invoices load from the Capital One sandbox. The forecast shows 14 healthy days. |
| 0:30–0:45 | **Safe payment.** Pay the $5k lease in the fake client: green `allow`. The inspector shows the call and the response in ~100 ms. "That's the whole integration: one API call." |
| 0:45–1:10 | **The catch.** Pay Northwind $40k: yellow. "Payroll on Nov 24 is short $18,400 if Acme pays 2 days late, and Acme was late on 3 of its last 5 invoices in Nessie." Three verified fixes, cheapest first. Click *Approve & apply* on the reserve transfer: a Nessie transfer ID appears, balances re-sync, "Reconciled: $0 difference", re-check is green. |
| 1:10–1:40 | **The agent's find.** Open Stress. A late Acme invoice is fine, a reserve outage is fine, both together are fatal. The infeasibility report names the binding constraint. Show the agent trace and the benchmark: "found it in N simulations; random search needed M." |
| 1:40–2:00 | **Close.** "One API call before every payment. The agent explores, the optimizer guarantees, the verifier double-checks, and Nessie executes. CashGuard: the fraud check, but for running out of cash." |

**Backup:** record the video ahead of time. Also keep fixture mode ready, because the badge will honestly say "Fixture mode" if Nessie is down.

---

## 12. Competitive landscape

| Category | Examples | What they do | What CashGuard adds |
|---|---|---|---|
| Enterprise treasury suites | Kyriba, HighRadius, FIS Quantum | Forecasting and dashboards for treasury teams | An embeddable developer API that runs automatically when a payment is made; affordable for SMBs |
| AP automation / forecasting | Tesorio, Medius | Simulate payment timing; show the effect on the forecast | Returns an optimized, verified fix, not just an updated chart |
| Consumer affordability APIs | Flinks, Fiskil | "Can this person afford this payment?" | Multi-account business cash with settlement delays, routes and credit |
| Corporate cards | Brex, Ramp, Rho | Card limits tied to today's balance | Looks forward, explains why, and works for any payment rail |

**Positioning:** balance checks exist for consumers and dashboards exist for enterprises. Nobody gives developers one call that answers "will this payment break us next week, and what's the cheapest fix?"

**Why judges should trust it:** most "AI treasury" tools say "our model predicts". CashGuard can say "here is a plan that satisfies every constraint, re-checked in exact cents", or "no such plan exists, and here's why."

---

## 13. Judge Q&A

| Question | Answer |
|---|---|
| "Doesn't Kyriba / HighRadius do this?" | They're enterprise dashboards. CashGuard is a developer API that runs inside the apps where payments happen, and it returns verified fixes, not charts. |
| "Isn't this just an if-statement on the balance?" | No. Today's balance is fine in the demo. The breach is 4 days out and only happens when a late invoice combines with settlement timing. That takes simulation plus optimization. |
| "Why use AI at all?" | The optimizer provides correctness. The agent finds dangerous shock combinations people wouldn't think to test, and we measured it against random search. The LLM writes plain-English explanations that are checked against the numbers. |
| "Can you trust an LLM with money?" | The LLM never decides or moves money. The plans come from the LP, every plan is replayed in exact cents, and every write needs human approval. |
| "How does Nessie fit?" | Nessie is the bank: balances, bills, payees and payment history come from it, and approved fixes run as Nessie transfers and bills, then get reconciled. In production, Plaid or bank APIs would plug into the same snapshot format. |
| "Latency?" | The check is a few simulations plus a small LP, under 300 ms. The agent runs at sync time and its results are cached. |

---

## 14. HackPrinceton tracks

| Track | How we fit |
|---|---|
| **Business & Enterprise** (main) | A B2B financial tool for SMBs: an API plus a dashboard |
| **Capital One: Best Usage of Nessie** | Reads (accounts, bills, deposits, merchants, history), writes (transfers, bills), reconciliation, and Nessie history driving the risk model |
| **Amazon: Best Practical AI Innovation** | An agent that observes, searches, plans and acts behind an approval gate; adaptive search measured against random; real-world use |
| **LangChain: Best use of deepagents** | Orchestrator with simulate, optimize and search tools, plus one subagent per shock family |
| **ChatOverflow** | Connect a coding agent for the raffle |

---

## 15. Safety and correctness checklist (pytest)

- [ ] No-op simulation leaves balances unchanged *(exists)*
- [ ] Internal transfers conserve total cash including in-flight amounts *(exists)*
- [ ] A destination balance never increases before settlement *(exists)*
- [ ] A pending Nessie deposit is never counted as posted cash
- [ ] Every returned fix passes integer-cent replay
- [ ] The planner returns an explicit infeasible result, never made-up amounts *(exists)*
- [ ] `apply` without `approved_by` → 403; with writes disabled → simulated only
- [ ] A repeated `Idempotency-Key` → no second Nessie write
- [ ] The same snapshot, seed and request → an identical decision
- [ ] Held-out seeds are separate from search seeds
- [ ] The explainer's grounding check rejects an invented number
- [ ] The Nessie mapper rejects an unknown schema with a tagged error

---

## 16. Roadmap

- **Learning:** update customer reliability from real settlement history.
- **Probabilistic mode:** Monte Carlo over receipt timing to give a probability of breach, not just a worst case.
- **Payment rails:** card-authorization mode with a latency budget, ACH batch checks, payroll-provider integrations.
- **Policy engine:** plain-English rules (e.g. "payroll never below 1.2× the next run") compiled into LP constraints.
- **Benchmarks:** anonymized comparisons, such as "companies like yours keep a 9-day buffer".

---

## 17. Rules and disclaimers

- **Repo rule:** the hackathon requires a public repo created between Nov 13 and Nov 15. This repo's commits are dated Nov 14 (`felipechala/TreasuryTwin`). Confirm that everyone who contributes is on the team.
- Synthetic data only. Not financial advice. No real money moves. Nessie is a sandbox, and writes are off by default.

## One-liners

- **Pitch:** One API call before every payment that tells you if it'll break you next week, and the cheapest way to make it safe.
- **Tagline:** The fraud check, but for running out of cash.
- **Technical:** The agent explores, the optimizer guarantees, the verifier double-checks, and Nessie executes.

## References

- TreasuryTwin repo: https://github.com/felipechala/TreasuryTwin
- Capital One Nessie: http://api.nessieisreal.com · docs: https://nessieisreal.com/docs
- Nessie endpoint inventory: https://github.com/Kevin-SalazarG/Capital-One/blob/main/nessie-node-sdk/docs/endpoints.md
- Flinks payment attributes: https://docs.flinks.com/api/enrich/endpoints/payment-attributes.md
- HighRadius: https://www.highradius.com/?p=219308
- Tesorio AP forecasting: https://www.tesorio.com/blog/how-to-automate-ap-forecasting-and-forecast-cash-flow-in-real-time
- Corpay AP cash-flow calendar: https://www.corpay.com/fr-CA/resources/blog/ap-cash-flow-calendar-dso-delay
- Brex credit limits: https://brex.com/support/brex-credit-limits
- Kognitos 2026 treasury tools: https://www.kognitos.com/blog/best-ai-tools-treasury-liquidity-management-2026/
