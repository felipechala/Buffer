# CashGuard — combined build plan (v2)

> **Stripe Radar stops fraud before a payment goes through. CashGuard stops a cash crunch before a payment goes through.**

CashGuard is the API surface of the **TreasuryTwin** engine. Payroll, invoicing, bill-pay and card apps call it **before money moves**. It simulates the payment against the company's whole multi-account cash future and returns one answer:

1. **Safe?** Will this payment push any account below its buffer in the next 14 days, including under plausible shocks?
2. **What breaks, and when?** For example, "Payroll on Nov 24 is short $18,400 if Acme's invoice is 2 days late."
3. **The cheapest fix.** Move the payment date, transfer between accounts, or draw on credit. The fix comes from an optimizer, is replayed in exact cents, and is scored on held-out scenarios. If no fix exists, CashGuard says so and names the binding constraint.

It connects to the finance stack a company already uses. In the hackathon that is **Capital One Nessie** (balances, bills, deposits, payees, history, and sandbox execution). Ramp, Stripe and QuickBooks are future connectors behind the same interface.

**The website is a demo, not the product.** It shows a mock third-party bill-pay app calling the API, next to a live inspector that shows every request and response. The API is what we're selling.

*Synthetic data only. Not financial advice. No real money moves. Nessie is a sandbox.*

---

## 0. Where this plan comes from, and what changed

This plan merges the **TreasuryTwin v3 guide** (`TreasuryTwin_API_First_Financial_Intelligence_Build_Guide_v3.pdf`, "API-first financial intelligence infrastructure") with the **CashGuard pitch** (one call before every payment). The v3 guide replaced the v2 finance-ops-app guide and already points in CashGuard's direction, so the two mostly agree.

**Decisions made on Nov 14 when merging:**

| Topic | Decision |
|---|---|
| Workflow layer (SQLite, bills, invoices, approvals, policy, audit) that now exists in the repo | **Use it as the backing store.** Nessie sync fills it; the demo client's bill list is the store; the hero endpoint wraps `analyze_bill_impact`. |
| Endpoint naming | **v3 names.** The hero call is `POST /v1/twin/simulations` with a `proposed_action`. CashGuard's `allow / warn / block` contract lives inside its response. |
| Connector design | **v3 `FinanceConnector` protocol** (read-only) plus a separate, gated sandbox-action connector. Nessie and the fixture both implement it. |
| Positioning | "Connect the stack you already use." Nessie is built; Ramp / Stripe / QuickBooks are roadmap connectors. |
| Evaluation | **v3's A/B/C protocol** (pay-earliest + buffer transfers vs. LP only vs. adaptive search + LP) on one held-out set, replacing the narrower adaptive-vs-random benchmark. |
| Agent | Deterministic adaptive search is the core. **deepagents stays** as an orchestration layer on top, on the cut list. |
| Demo UI | **Mock third-party client + API inspector.** The Pay button lives in the mock app (v3's "one use from a separate client"). Execution is behind "Approve sandbox action". |
| Demo fixture | A **new 6-account company** (`fixtures/cashguard_demo.json`) with a 14-day horizon, seeded into Nessie. Northstar stays for engine tests. |

### What already exists in the repo (as of commit `c71406e`)

| Area | Exists | Reused by CashGuard as |
|---|---|---|
| `engine/models.py`, `engine/twin.py` | Integer-cent model, hashed immutable `TwinState`, overrides | The snapshot |
| `engine/simulator.py` | Day-by-day simulator: settlement lag, in-transit cash, credit delay, shocks | Baseline, stress and verification replays |
| `engine/optimizer.py` | **Scenario-robust** HiGHS LP (`optimize_plan(twin, stress=[...])`), `stress_checks`, `actionable`, infeasibility report with binding constraints | Fix generator |
| `engine/scenarios.py` | Correlated Monte Carlo (`sample_scenarios`), `adverse_scenario(quantile)`, `scenario_probability`, discovery vs held-out seed bases | Verifier, plausibility weights |
| `services/impact.py` | **Pay-date enumeration** + nominal/robust plans + held-out breach rate + feasible / requires_review / infeasible | The core of `/v1/twin/simulations` |
| `workflows/` | SQLite `Store`, append-only audit, AR/AP state machines, spend policy, approval gate (finance lead, stale-snapshot rejection), `build_twin_from_store`, demo seed | Backing store; sync target; audit log |
| `main.py` `/api/*` | AR/AP CRUD, `/api/bills/{id}/impact`, `/api/plans`, `/api/plans/{id}/approve`, `/api/forecast`, `/api/audit`, `/api/demo/reset`, `X-User-Id` roles | Internal routes; the public `/v1` layer sits on top |
| `fixtures/` | `demo_company.json` (Northstar, 3 accounts, 7 days), `engine/golden.yaml`, `engine/northstar_example.yaml`, `engine/halcyon_stress.yaml` | Engine tests |

**Not built yet:** the `/v1` public API and decision contract; forbid-one-action fix variants; Nessie connector + sync + mapper; sandbox executor; the risk-search agent; explainer; the 14-day demo fixture; the frontend; the evaluation script.

---

## 1. The problem

Companies with several bank accounts (operating, payroll, reserve, credit line) approve payments one at a time. Cash problems come from **how those payments combine**:

- A $40k vendor payment looks fine today. But a customer invoice arrives late, a reserve transfer takes time to settle, and payroll hits Thursday. Together, payroll bounces.
- The people approving payments see today's balance, not the next 14 days.
- Treasury suites that model this (Kyriba, HighRadius, Tesorio) are enterprise dashboards. They don't run as a check inside the tools where payments happen.
- SMBs rely on spreadsheets and a manual rule: "release a payment if the post-payment balance stays above the floor."

The results are overdrafts, bounced payroll, emergency credit draws and late fees, all predictable days in advance.

---

## 2. The API (the product)

### 2.1 Hero call: `POST /v1/twin/simulations`

Simulate a proposed action against the current snapshot and return a decision with fixes. The action can reference a bill already in the store (synced from Nessie) or be ad hoc.

```http
POST /v1/twin/simulations
Authorization: Bearer cg_test_demo_key
Content-Type: application/json

{
  "snapshot_id": "snap_9f2c…",            // optional; default = latest
  "proposed_action": {
    "kind": "payment",
    "bill_id": "BILL-NW-4471",             // or amount_cents/from_account/payee
    "pay_date": "2026-11-20",
    "latest_date": "2026-11-27",
    "priority": "normal"
  },
  "scenario": null,                        // optional judge override, e.g. {"delay_receivable_id": "INV-4471", "delay_days": 3}
  "horizon_days": 14,
  "risk_tolerance": 0.05
}
```

```json
{
  "run_id": "run_7c1a…",
  "decision": "warn",
  "safe": false,
  "breaks": [
    {
      "date": "2026-11-24", "account": "payroll", "obligation": "Biweekly payroll",
      "shortfall_cents": 1840000,
      "scenario": { "id": "acme_late_2d", "label": "Acme invoice 4471 arrives 2 days late", "plausibility": 0.6 }
    }
  ],
  "fixes": [
    { "plan_id": "plan_a1…", "summary": "Pay Northwind on Nov 25 instead of Nov 20", "cost_cents": 0, "delay_days": 5,
      "actions": [{ "kind": "schedule_bill", "bill_id": "BILL-NW-4471", "pay_day": 5 }],
      "verified": true, "held_out": { "breach_rate": 0.0, "paths": 200 } },
    { "plan_id": "plan_b2…", "summary": "Pay Nov 20; transfer $18.4k reserve → payroll on Nov 21", "cost_cents": 920,
      "actions": [{ "kind": "reserve_transfer", "route": "reserve-to-payroll", "amount_cents": 1840000, "dispatch_day": 1, "arrival_day": 2 }],
      "verified": true, "held_out": { "breach_rate": 0.02, "paths": 200 } },
    { "plan_id": "plan_c3…", "summary": "Pay Nov 20; draw $18.4k on the revolver", "cost_cents": 23000,
      "actions": [{ "kind": "credit_draw", "line": "revolver", "amount_cents": 1840000, "day": 3 }],
      "verified": true, "held_out": { "breach_rate": 0.0, "paths": 200 } }
  ],
  "curves": { "before": { "operating": [...], "payroll": [...] }, "after": { "plan_a1…": { ... } } },
  "constraint_report": ["payroll held exactly at its $25,000 buffer from day 4."],
  "explanation": "Paying $40,000 on Nov 20 is safe if Acme pays on time, but if invoice 4471 arrives 2 days late, payroll on Nov 24 is short $18,400. Moving the payment to Nov 25 avoids this at no cost.",
  "provenance": {
    "snapshot_id": "snap_9f2c…", "input_digest": "sha256:…", "engine_version": "optimizer-lp-0.2",
    "scenario_seed": 1000042, "stress_set": "stress_4d2e… (24 scenarios)", "source": "nessie", "synced_at": "2026-11-20T14:02:11Z"
  },
  "latency_ms": 180
}
```

Numbers above are examples. The golden tests on the demo fixture pin the real values (§7).

### 2.2 Decision levels

| Decision | Rule | Default behavior in the calling app |
|---|---|---|
| `allow` | The requested date needs **no actions**: no breach in the baseline, none in the cached stress set, held-out breach rate ≤ `risk_tolerance` | Proceed |
| `warn` | Something breaches (baseline or stress) **and at least one verified fix exists**; or it is safe only with `requires_review` residual risk | Show reason and fixes; a person chooses |
| `block` | The baseline breaches **and no fix exists** on any legal date within constraints | Hold; infeasibility report attached |

A breach that appears only under a stress scenario and has no fix is `warn` with `fixes: []` and the report. We don't block on a hypothetical alone.

### 2.3 Endpoints (v3 naming)

| Endpoint | Purpose | Backed by |
|---|---|---|
| `GET /v1/connections` | Integration health: provider, scope, status, `last_sync_at`, mode (`nessie` / `fixture`) | connector `health()` |
| `POST /v1/connections/nessie/sync` | Read Nessie, normalize into the store, build a new snapshot, kick off a background stress run. Returns `sync_id`, `snapshot_id`, `source_count` | §5 |
| `POST /v1/connections/fixture/sync` | Same, from the fixture connector (fallback / offline) | §5 |
| `GET /v1/twin/state` | Current reconciled state: accounts with `as_of` + source badge, upcoming obligations, expected receipts, `snapshot_id`, freshness | `build_twin_from_store` |
| `POST /v1/twin/forecasts` | 14-day projection per account: baseline, p10/p50/p90 from Monte Carlo, worst discovered shock, buffer lines | `sample_scenarios`, stress cache |
| `POST /v1/twin/simulations` | **Hero.** Decision + fixes for a proposed action; or, with only `scenario`, a before/after counterfactual for judge changes | `analyze_bill_impact` + §4 |
| `POST /v1/twin/simulations/batch` | Check a payment run; return the largest safe subset today (stretch) | greedy by priority, re-checked |
| `POST /v1/twin/stress-tests` | Start the bounded risk search → `202 {run_id}` | §6 |
| `GET /v1/twin/stress-tests/{id}` | Ranked danger list, infeasibility reports, agent trace, seed | §6 |
| `POST /v1/twin/plans` | Compute a plan for an explicit objective/permission set (power-user route) | `optimize_plan(stress=...)` |
| `GET /v1/plans/{id}` | Plan with solver status, stress checks, held-out rate | store |
| `POST /v1/plans/{id}/approve` | Human approval bound to the plan's snapshot hash. `409` if the snapshot changed | existing approval gate |
| `POST /v1/plans/{id}/execute-sandbox` | Gated Nessie write. `Idempotency-Key` required. Returns provider refs and `confirmed` / `uncertain` / `simulated` | §5.4 |
| `GET /v1/events` | Append-only decision/audit log: runs, approvals, executions, reconciliations | `Store.audit` |

The existing `/api/*` routes stay as the internal workflow API; `/v1` is the public contract the demo client and the inspector use.

### 2.4 Conventions

- **Auth:** `Authorization: Bearer cg_test_…` (one demo tenant token from `.env`). Internal `/api/*` keeps `X-User-Id`.
- **Money:** integer cents. **Dates:** ISO `YYYY-MM-DD`, converted to day indexes against the snapshot's `as_of` (`day_index`).
- **Provenance on every response:** `snapshot_id`, `input_digest`, `engine_version`, `scenario_seed`, assumption list.
- **Status codes:** `409` stale snapshot / plan; `422` invalid constraints; `202` async stress run; `503` provider unavailable with `mode: fixture` suggested.
- **Errors:** `{ "error": { "type": "...", "message": "...", "param": "..." } }`.
- **Idempotency:** `Idempotency-Key` on approve and execute. A repeated key returns the stored result, never a second write.
- **Never return an actionable fix** if the solver, replay or verifier failed.
- **OpenAPI** at `/docs` is the developer documentation.

---

## 3. Architecture

```
 Mock client "Acme Payroll & Bills"            API inspector (same page)
          │  POST /v1/twin/simulations ──────────► request / response / latency / curl
          ▼
 ┌──────────────────────────────────────────────┐
 │ CONNECTORS (read-only FinanceConnector)      │  nessie.py · fixture.py        → health()
 │ + SandboxActionConnector (gated writes)      │  actions/gateway.py
 └──────────────┬───────────────────────────────┘
                ▼
 ┌──────────────────────────────────────────────┐
 │ NORMALIZE + STORE (exists: workflows/)       │  accounts·bills·invoices·cash events·audit
 │ dedupe by (provider, object id); provenance  │  treasury overlay: buffers, routes, credit
 └──────────────┬───────────────────────────────┘
                ▼
 ┌──────────────────────────────────────────────┐
 │ VERSIONED TWIN (exists)                      │  build_twin_from_store → hashed TwinState
 │ observed | committed | hypothetical          │
 └──────────────┬───────────────────────────────┘
                ▼
 ┌──────────────────────────────────────────────┐
 │ ENGINE (exists) + AGENT (new)                │  simulate · Monte Carlo · robust LP
 │                                              │  adaptive stress search (+ deepagents wrapper)
 └──────────────┬───────────────────────────────┘
                ▼
 ┌──────────────────────────────────────────────┐
 │ DECISION LAYER (new)                         │  impact → fixes → verify → allow/warn/block
 │ explainer (LLM, grounded)                    │
 └──────────────┬───────────────────────────────┘
                ▼
      /v1 API ──► events log ──► (human approves) ──► execute-sandbox ──► re-sync ──► reconcile
```

**Three state layers are never mixed:** observed (Nessie balances and posted events, with `as_of`), committed (approved bills, expected receivables, approved plans), hypothetical (shocks, pay dates, optimizer actions). No simulated transfer ever enters the observed ledger.

**Rule:** the AI never decides and never moves money. The search proposes scenarios; the LP produces plans; the simulator verifies them; a person approves; only the gateway writes to Nessie, in sandbox.

---

## 4. How the hero call works

### 4.1 Snapshot

`build_twin_from_store` already derives a hashed `TwinState` from the store. Sources:

- **Nessie** (via sync): observed balances with `as_of`, scheduled bills → `APBill`, pending deposits → issued `ARInvoice`, completed deposits → `CashEvent` and payment history.
- **Treasury overlay** (`fixtures/cashguard_treasury.yaml`, loaded into `CompanyConfig`): buffers, routes (settlement days, caps, fees, allowed), credit terms, nondeferrable categories, shock bounds, Nessie nickname → account-id map. Labeled as customer-specified assumptions, never as provider facts.

### 4.2 Decision logic

```
1. Resolve the action: a stored bill, or an ephemeral APBill (not persisted) for ad-hoc payments.
2. impact = analyze_bill_impact(bill, candidate days = requested … latest, held-out seed B)
   → per date: no-action replay, nominal plan, robust plan (adverse quantile), held-out rates.
3. Stress set = cached stress-test result for this snapshot (top-K discovered shocks + single shocks
   with plausibility ≥ threshold). Replay the requested date, no actions, under each.
4. allow  if the requested date needs no actions, nothing breaches, held-out rate ≤ tolerance.
5. Otherwise build fixes (4.3), verify (4.4), rank by cost.
6. warn   if any verified fix exists (or requested date is feasible only with requires_review).
   block  if the baseline breaches and every date/plan is infeasible → attach violations + binding constraints.
7. Explain (4.5). Persist an AnalysisRun; append an event. Return.
```

The stress set is produced by `/v1/twin/stress-tests` on every sync and cached by snapshot hash, so the hero call never waits on the agent.

### 4.3 Fix generator

| Fix family | Method | Status |
|---|---|---|
| **Reschedule** | `candidate_days` enumeration from `impact.py`; earliest later date whose no-action replay is clean under baseline + stress set | exists |
| **Cheapest funding on the requested date** | `optimize_plan(twin)` (nominal) and `optimize_plan(twin, stress=[adverse, *top stress])` (robust) | exists |
| **No credit** | re-solve with `disabled_credit_lines` | new |
| **No transfers** | re-solve with routes disabled (credit only) | new |
| **Reschedule + funding** | the impact option on a later date that still needs a small plan | exists |

Deduplicate, rank by `modeled_cost_cents` then held-out rate then later date, return the top 3. Each fix is a stored `Plan` so it can be approved and executed by id. If every option is infeasible, return the LP's violations and `_explain_shortfall` text.

### 4.4 Verifier

Already mostly built: `optimize_plan` replays every plan under each scenario in exact cents (`stress_checks`), and `impact.py` scores frozen plans on held-out Monte Carlo seeds (`HELDOUT_SEED_BASE`, never used for planning). CashGuard adds one step: replay each fix under the **discovered stress set** too. `verified: true` requires a clean baseline replay and a clean stress-set replay; the held-out breach rate is reported and drives `requires_review`.

### 4.5 Explainer

One LLM call (`CASHGUARD_EXPLAIN_MODEL`, default `claude-haiku-4-5-20251001`) receives only the structured result (breaks, scenario label, fixes, binding constraints) and returns one or two sentences. **Grounding check:** every money amount and date in the text must appear in the payload; on mismatch, timeout or missing key, fall back to a template sentence. The demo never depends on the LLM.

---

## 5. Connectors and Nessie

### 5.1 Connector contract (`backend/app/connectors/`)

```python
class FinanceConnector(Protocol):           # read-only
    async def fetch_accounts(self) -> list[RawAccount]: ...
    async def fetch_transactions(self, cursor=None) -> Page: ...   # deposits, withdrawals, transfers
    async def fetch_obligations(self, cursor=None) -> Page: ...    # bills
    async def fetch_counterparties(self) -> list[RawMerchant]: ...
    async def health(self) -> ConnectionHealth: ...

class SandboxActionConnector(Protocol):     # separate, gated by SANDBOX_WRITES_ENABLED
    async def create_transfer(self, req: TransferRequest, idempotency_key: str) -> ProviderRef: ...
    async def schedule_bill(self, req: BillRequest, idempotency_key: str) -> ProviderRef: ...
```

`connectors/nessie.py` and `connectors/fixture.py` implement the read contract; `actions/gateway.py` wraps the write contract. The normalizer (`ingest/normalize.py`) turns raw records into store objects and is a pure function tested against recorded JSON in `fixtures/nessie/`. The engine never knows where a bill came from. Ramp, Stripe and QuickBooks are future implementations of the same protocol.

### 5.2 Setup

- `.env`: `NESSIE_API_KEY`, `NESSIE_BASE_URL` (confirm the active host in the docs; `api.nessieisreal.com` or `prod.nessieisreal.com`), `SANDBOX_WRITES_ENABLED=false`.
- Auth is the `?key=` query parameter. The key never reaches the frontend, logs or git.
- **Hour 0:** verify host, key and the first `GET /accounts`; save one sanitized response per endpoint to `fixtures/nessie/`.

### 5.3 Seeding (`scripts/seed_nessie.py`)

Creates the demo company in Nessie from `fixtures/cashguard_demo.json`. Idempotent (matches on nickname; writes `fixtures/nessie_ids.json`, gitignored).

| What | Nessie call | Content |
|---|---|---|
| Company | `POST /customers` | "Acme Co (synthetic)" |
| Accounts | `POST /customers/{id}/accounts` | operating, payroll, tax, reserve, settlement, intl_clearing |
| Payees | `POST /merchants` | Northwind Supplies, Initech, landlord, insurer, … |
| Scheduled outflows | `POST /accounts/{id}/bills` (`payment_date`, `payment_amount`, `payee`, `status`) | Payroll, rent, supplier bills inside 14 days |
| Expected inflows | `POST /accounts/{id}/deposits` (`status: pending`, future `transaction_date`, `description: "INV-4471 Acme Industrial"`) | Customer invoices |
| Payment history | completed deposits with past dates and `"… due YYYY-MM-DD"` descriptions | Acme late on 3 of 5; Globex on time |
| Reset | account-level deletes (verify what the sandbox allows) | Clean slate before the demo |

If Nessie rejects future-dated pending deposits, expected receipts move into the treasury overlay and are labeled as such.

### 5.4 Read path → store

| Nessie | Store object | Rule |
|---|---|---|
| account `balance` (dollars) | `CompanyConfig.accounts[].observed_balance_cents`, `provider: nessie`, `as_of` = sync time | cents at the boundary; nickname map |
| bill (pending / recurring, in horizon) | `APBill` `status: approved` (or `scheduled` if it has a date), `source: nessie`, `provider_id` | priority / deferrability from the overlay's category rules |
| pending deposit (future) | `ARInvoice` `status: issued`, `expected_receipt_date`, customer from the description | **forecast, not cash** |
| completed deposit / withdrawal | `CashEvent` with `included_in_snapshot: true` | dedupe by `(provider, object id)` |
| completed deposit with a `due` date | customer payment history → `delay_distribution` | §5.6 |
| merchants | payee directory | validates `payee` in ad-hoc actions |
| loans | shown as context only | **credit availability is never inferred from Nessie** |

Every synced record carries `provider`, `provider_id`, `observed_at`. Re-sync upserts; nothing is double-counted.

### 5.5 Write path: `POST /v1/plans/{id}/execute-sandbox`

Preconditions: plan `actionable`, approved, snapshot hash still current (else `409`), `Idempotency-Key` present, `SANDBOX_WRITES_ENABLED=true` (else the response is `simulated` and labeled).

| Plan action | Nessie write | UI label |
|---|---|---|
| `reserve_transfer` / transfer | `POST /accounts/{from}/transfers` (`medium: balance`, `payee_id`, `amount`, `transaction_date`, `description: "TreasuryTwin plan_…"`) | "Executed in Nessie sandbox" + transfer id |
| `schedule_bill` | update the bill's `payment_date` (`PUT /bills/{id}`) | "Rescheduled in Nessie sandbox" + bill id |
| `credit_draw` | not representable in Nessie → internal `CashEvent` labeled simulated | "Simulated credit draw" |

After any write: re-sync → new snapshot → **reconcile** (expected vs. observed balances; in-flight amounts explained by route `settlement_days`; show "Reconciled: $0 unexplained") → re-run the original simulation, which should now be `allow`. A timeout leaves the execution `uncertain`; it is re-read, never retried blindly.

### 5.6 Customer reliability from history

Per customer: on-time share and mean delay from completed deposits vs. their `due` dates → a `delay_distribution` on the `Customer` record, which `scenario_probability` and the search use for plausibility. This is computed from **seeded synthetic history**; we don't claim calibration.

### 5.7 Failure handling

| Condition | Behavior |
|---|---|
| 401 / 403 | `GET /v1/connections` shows `error`; keep the last snapshot or switch to the fixture connector; never invent data |
| 429 / 5xx / timeout | bounded read retries; stale `synced_at` shown; explicit fixture mode |
| unknown schema | normalizer fails with a tagged error and a redacted sample |
| ambiguous write | `uncertain` until re-read |
| writes disabled | `simulated` execution, labeled |

---

## 6. Risk-search agent

### 6.1 Deterministic core (`agent/shocks.py`, `agent/search.py`, no LLM)

- **Shock vector** (bounds in the overlay): receipt delay 0–5 days per customer; collection fraction 70–100%; surprise outflow $0–20k; one route outage; credit delayed/unavailable.
- **Score** = breach severity (shortfall cent-days, missed nondeferrable obligations) × plausibility (`scenario_probability`) − small severity penalty + novelty. Prefer the **least severe plausible shock that breaches**.
- **Search:** v3's elite mutation loop: ~20 random plausible shocks, then mutate elites within bounds; keep the top-10 unique; every evaluation logged with its reason. Budget-bounded (`budget=120`). Discovery seeds from `DISCOVERY_SEED_BASE`.
- **Output:** ranked danger list, each with shocks, first breach, shortfall, plausibility, whether a robust plan exists (`optimize_plan(stress=[shock])`), and the infeasibility report when it doesn't.
- **The key finding:** compound shocks. Acme late alone is survivable; the reserve route down alone is survivable; both together are infeasible.

### 6.2 deepagents layer (`agent/deep.py`, optional, on the cut list)

LangChain deepagents orchestrator (`CASHGUARD_AGENT_MODEL`, default `claude-sonnet-5-5`) with tools `search_shocks(budget, focus)`, `simulate`, `optimize`, `list_shocks`, `get_history(customer)`, and one subagent per shock family. It chooses where to focus the deterministic search and writes the trace narrative. If the LLM is unavailable, the stress-test endpoint runs the core alone.

### 6.3 Evaluation (`scripts/evaluate.py`), v3 protocol

- Fixed suite: the demo fixture + ~10 generated fixtures, ≥100 held-out scenarios each, documented seeds and bounds.
- Policies: **(A)** pay on earliest date + simple buffer-threshold transfers; **(B)** LP without risk search; **(C)** adaptive search + robust LP.
- Report: buffer breaches / missed obligations, total modeled cost, worst shortfall, unique failures found by search vs. equal-budget random, runtime. Raw numbers in the README and one chart in the Stress tab. Show a genuine infeasible case.

---

## 7. Demo fixture (`fixtures/cashguard_demo.json`)

Same shape as `demo_company.json` (config + customers + vendors + invoices + bills + cash_events) so `seed_demo` loads it. Six accounts, `as_of` 2026-11-20, 14-day horizon, **reserve→payroll route allowed** (1-day settlement). Seeded into Nessie by §5.3. Tuned until these golden tests pass (`tests/test_cashguard_golden.py`):

| Test | Expected |
|---|---|
| $5,000 lease, Nov 20 | `allow` |
| $40,000 to Northwind, Nov 20 | `warn`; baseline clean; under "Acme 4471 two days late" payroll breaches on Nov 24; fixes ordered reschedule → reserve transfer → credit draw, all `verified: true` |
| Approve + execute the reserve transfer, re-simulate | `allow`; reconciliation diff = 0 |
| Stress: Acme late alone / reserve route down alone | survivable |
| Stress: both together, with the $40k paid | infeasible; report names the binding constraint |
| $250,000 bill, Nov 20 | `block` |
| Every returned fix | clean exact-cent replay under baseline + stress set |
| Stale plan | approve after a new sync → `409` |

---

## 8. The website: a demo, not the product

**Stack:** Vite + React + TypeScript + Tailwind + Recharts. One page, three regions. Every control calls `/v1`; nothing is decorative.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ TreasuryTwin · CashGuard demo   ● Nessie sandbox · synced 12s ago  [Sync]    │
├───────────────────────────────────┬──────────────────────────────────────────┤
│  ACME PAYROLL & BILLS (mock client)│  API INSPECTOR                           │
│  (styled as someone else's app)    │  POST /v1/twin/simulations   180 ms 200  │
│  Northwind Supplies  $40,000 [Pay] │  ▸ request  { proposed_action: … }       │
│  Office lease         $5,000 [Pay] │  ▸ response { decision: "warn", … }      │
│  Big equipment      $250,000 [Pay] │  [curl] [Python] [JS]                    │
│  ┌ ⚠ Needs review ───────────────┐ │  History: sync · sim · sim · approve ·   │
│  │ Payroll Nov 24 short $18,400  │ │           execute-sandbox · sync         │
│  │ if Acme pays 2 days late.     │ │                                          │
│  │ ① Pay Nov 25          $0      │ │                                          │
│  │ ② Reserve → payroll   $9.20   │ │                                          │
│  │ ③ Draw revolver       $230    │ │                                          │
│  │ [Approve sandbox action ②]    │ │                                          │
│  └───────────────────────────────┘ │                                          │
├───────────────────────────────────┴──────────────────────────────────────────┤
│ Tabs: [Twin state] [Forecast] [Risk lab] [Events]                            │
│  Twin state: accounts (observed badge + as_of), obligations, receipts        │
│  Forecast: 14-day curves per account, p10/p50/p90, buffer lines, worst shock │
│  Risk lab: judge controls (delay a receivable, change a bill) → re-simulate; │
│            danger list, infeasibility report, agent trace, A/B/C chart       │
│  Events: runs, approvals, executions, Nessie refs, reconciliation            │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **Left, the mock client.** Clearly a different brand. Pay → `/v1/twin/simulations` → green / yellow / red card. "Approve sandbox action" → `/v1/plans/{id}/approve` → `/v1/plans/{id}/execute-sandbox` → Nessie ref shown → re-sync → re-simulate → green.
- **Right, the inspector.** The hero of the page: every call live, with latency and copy-as-curl/Python/JS.
- **Risk lab** is v3's "critical interaction": a judge delays a receivable or changes a bill amount, the chart updates from the backend, and a new plan is requested.
- **Mode badge** always shows `Nessie sandbox` or `Fixture mode`; observed vs. assumed values are labeled.
- Developer docs = `/docs` + README quickstart. Stretch: `<cashguard-check>` web component wrapping the result card.

---

## 9. Repo layout (additions)

```
backend/app/
  api/v1.py                  # /v1 public routes (§2.3), bearer auth, error envelope
  services/decision.py       # decision logic (§4.2) on top of services/impact.py
  services/fixes.py          # forbid-one-action variants, ranking, stress-set replay
  services/explain.py        # LLM explainer + grounding check
  connectors/base.py         # FinanceConnector / SandboxActionConnector protocols
  connectors/nessie.py       # httpx client, retries, error mapping
  connectors/fixture.py      # deterministic provider from fixtures/*.json
  ingest/normalize.py        # raw → store objects, dedupe, provenance
  ingest/history.py          # customer reliability from completed deposits
  actions/gateway.py         # execute-sandbox, idempotency, reconcile
  agent/shocks.py, agent/search.py, agent/deep.py
backend/tests/
  test_cashguard_golden.py, test_v1_api.py, test_normalize.py, test_search.py, test_gateway.py
fixtures/
  cashguard_demo.json        # 6-account demo company (§7)
  cashguard_treasury.yaml    # buffers, routes, credit, shock bounds, nickname map
  nessie/                    # recorded sanitized responses
scripts/
  seed_nessie.py, evaluate.py
frontend/                    # Vite + React demo (§8)
```

`.env.example` additions: `CASHGUARD_API_KEY`, `ANTHROPIC_API_KEY`, `CASHGUARD_AGENT_MODEL`, `CASHGUARD_EXPLAIN_MODEL`. `requirements.txt` additions: `deepagents`, `langchain-anthropic`, `anthropic`.

---

## 10. Build plan (~19 hours to the 8:00 AM deadline)

| Block | Hours | Backend | Integrations (Nessie) | AI | Frontend |
|---|---|---|---|---|---|
| 1 | 0–3 | `cashguard_demo.json` + treasury overlay + golden tests; `/v1/twin/simulations` wrapping `analyze_bill_impact` with the decision contract; `/v1/twin/state`, `/v1/connections/fixture/sync` | Verify host/key; record fixtures; `FinanceConnector` + Nessie read client; `seed_nessie.py` | Shock library + deterministic search skeleton | Vite app; mock client + inspector; Pay → simulations |
| 2 | 3–7 | Forbid-one fix variants; stress-set replay in verifier; `/v1/twin/forecasts`; `/v1/events`; runs persisted | `normalize.py` + `/v1/connections/nessie/sync` + `/v1/connections`; dedupe tests | Elite mutation search + plausibility from history; `/v1/twin/stress-tests` (202) + cache wired into decisions | Result card, fixes, Twin state + Forecast tabs, mode badge |
| 3 | 7–10 | `/v1/plans/{id}/approve` (409 stale) + `execute-sandbox` + idempotency | Gateway writes (transfer, bill), re-sync, reconcile | deepagents layer + trace; explainer + grounding | Approve → execute → green flow; Risk lab judge controls; Events tab |
| — | 10–14 | Sleep in shifts. **Feature freeze at hour 14.** | | | |
| 4 | 14–17 | Latency, error envelopes, polish | Clean re-seed + dry run | `evaluate.py` + A/B/C chart | Polish, copy-as-curl; stretch: batch / widget |
| 5 | 17–18.5 | README (quickstart, curl, benchmarks, limitations), Devpost, 2-minute video, push | | | |

**Cut list** (top first): widget → batch → deepagents layer (keep deterministic search) → A/B/C chart (keep the search-vs-random number) → Nessie writes (keep reads; label execution simulated) → p10/p90 bands.

**Never cut:** `/v1/twin/simulations` with verified fixes, the inspector, the compound-shock finding, Nessie reads with an honest fallback.

---

## 11. Demo script (2 minutes)

| Time | Beat |
|---|---|
| 0:00–0:15 | **Problem.** "Companies already use Ramp and Stripe and their bank. We don't replace them. Businesses don't go under from one bad payment; they go under from two normal things in the same week." |
| 0:15–0:30 | **Connected.** Click *Sync*. Badge: Nessie sandbox, synced now. Six Acme Co accounts, bills and invoices from Capital One's sandbox; 14 healthy days on the chart. |
| 0:30–0:45 | **Safe payment.** Pay the $5k lease in the mock client: green `allow`. The inspector shows one call, ~150 ms. "That's the whole integration." |
| 0:45–1:10 | **The catch.** Pay Northwind $40k: yellow. "Payroll on Nov 24 is short $18,400 if Acme pays 2 days late, and Acme was late on 3 of its last 5 invoices." Three verified fixes. *Approve sandbox action* on the reserve transfer: Nessie transfer id, re-sync, "Reconciled: $0", re-check green. |
| 1:10–1:40 | **Risk lab.** Judge drags Acme's delay to 3 days: chart and plan recompute from the backend. The stress run found that Acme late + reserve route down is infeasible, neither alone is; the report names the binding constraint. "Random search needed N× the simulations." |
| 1:40–2:00 | **Close.** Show `/docs` and the curl. "The search explores, the optimizer guarantees, the replay double-checks, Nessie executes. TreasuryTwin: the fraud check, but for running out of cash." |

Backup: pre-recorded video; fixture mode ready and honestly labeled.

---

## 12. Competitive landscape and positioning

| Category | Examples | What they do | What we add |
|---|---|---|---|
| Enterprise treasury suites | Kyriba, HighRadius, FIS | Forecasting dashboards for treasury teams | An API that runs inside the apps where payments happen; SMB-affordable |
| AP automation / forecasting | Tesorio, Medius | Simulate payment timing, update a forecast | A verified, least-cost fix, not just a chart |
| Consumer affordability APIs | Flinks, Fiskil | "Can this person afford this payment?" | Multi-account business cash with settlement lags, routes and credit |
| Corporate cards / spend | Brex, Ramp, Rho | Limits tied to today's balance | Forward-looking, explains why, any rail; and we integrate with them rather than compete |

**Positioning:** "The predictive control plane for business finance: connect your existing stack, see cash consequences, and fix decisions before they become transactions." Balance checks exist for consumers and dashboards for enterprises; nobody gives developers one call that answers "will this payment break us next week, and what's the cheapest fix?"

**Why judges can trust it:** we say "here is a plan that satisfies every constraint, replayed in exact cents and scored on held-out scenarios," or "no such plan exists, and here's why."

---

## 13. Judge Q&A

| Question | Answer |
|---|---|
| "Doesn't Kyriba / HighRadius do this?" | They're dashboards for treasury teams. This is an API that sits inside bill-pay, payroll and card flows and returns verified fixes. |
| "Isn't this an if-statement on the balance?" | Today's balance is fine in the demo. The breach is four days out and only appears when a late receivable meets a settlement lag. That needs simulation plus optimization. |
| "Why AI?" | The optimizer gives correctness. The adaptive search finds shock combinations people don't test, and we measured it against random search at equal budget. The LLM only writes explanations that are checked against the numbers. |
| "Can you trust an LLM with money?" | It never decides or moves money. Plans come from the LP, every plan is replayed in cents, every write needs human approval bound to a snapshot hash. |
| "How does Nessie fit?" | Nessie is the bank: balances, bills, payees and history come from it; approved fixes execute as Nessie transfers and are reconciled. Ramp, Stripe and QuickBooks plug into the same connector interface later. |
| "Latency?" | The check is a few simulations plus a small LP, under ~300 ms. The stress search runs at sync time and is cached. |

---

## 14. HackPrinceton tracks

| Track | Fit |
|---|---|
| **Business & Enterprise** (main) | B2B financial infrastructure for SMBs |
| **Capital One: Best Usage of Nessie** | Reads, history-driven risk model, gated sandbox writes, reconciliation; Nessie is integral to the twin |
| **Amazon: Best Practical AI Innovation** | Adaptive hypothesis search with an auditable trace, measured against baselines, behind an approval gate |
| **LangChain: Best use of deepagents** | Orchestrator with simulate / optimize / search tools and per-shock-family subagents (optional layer) |
| **ChatOverflow** | Connect a coding agent for the raffle |

---

## 15. Safety and correctness checklist (pytest)

- [x] No-op simulation leaves balances unchanged
- [x] Internal transfers conserve total cash including in-flight
- [x] Destination never credited before settlement
- [x] Issued invoice is never posted cash
- [x] Planner returns explicit infeasible, never invented amounts
- [x] Held-out seeds disjoint from discovery seeds
- [x] Unapproved / stale plan cannot execute
- [ ] Duplicate Nessie object on re-sync → exactly one store record
- [ ] Every returned fix passes exact-cent replay under baseline + stress set
- [ ] Repeated `Idempotency-Key` → no second Nessie write
- [ ] Same snapshot + seed + request → identical decision and `input_digest`
- [ ] Explainer grounding check rejects an invented number
- [ ] Normalizer rejects an unknown schema with a tagged error
- [ ] Nessie unavailable → labeled fixture mode, no invented balances

---

## 16. Roadmap

- **Connectors:** Ramp (bills, approvals), Stripe (invoices, payouts; a paid invoice is not settled cash), QuickBooks (ledger identity matching), real bank APIs. Cross-provider reconciliation with explicit "unmatched" status.
- **Calibration:** reliability learned from real settlement history; probabilistic breach estimates with disclosed limits.
- **Rails:** card-authorization mode with a latency budget, ACH batch checks, payroll providers.
- **Policy engine:** plain-English rules compiled into LP constraints.
- **Platform:** SDKs, webhooks, tenant auth, policy versions, optional provider write-backs.

---

## 17. Rules and disclaimers

- The hackathon requires a public repo created between Nov 13 and Nov 15 (`felipechala/TreasuryTwin`, first commit Nov 14). Everyone who contributes must be on the team.
- Synthetic data only. Not financial advice. No real money moves. Nessie is a sandbox; writes are off by default. No partnership or endorsement by Capital One, Ramp, Stripe, QuickBooks or HackPrinceton is implied.

## One-liners

- **Pitch:** One API call before every payment that tells you if it'll break you next week, and the cheapest way to make it safe.
- **Tagline:** The fraud check, but for running out of cash.
- **Technical:** The search explores, the optimizer guarantees, the replay double-checks, Nessie executes.

## References

- TreasuryTwin repo: https://github.com/felipechala/TreasuryTwin
- Capital One Nessie docs: https://prod.nessieisreal.com/docs · getting started: https://prod.nessieisreal.com/getting-started
- Nessie endpoint inventory: https://github.com/Kevin-SalazarG/Capital-One/blob/main/nessie-node-sdk/docs/endpoints.md
- Ramp developer API: https://docs.ramp.com/developer-api · Stripe invoices: https://docs.stripe.com/api/invoices/list · QuickBooks: https://developer.intuit.com/app/developer/qbo/docs/
- Flinks payment attributes: https://docs.flinks.com/api/enrich/endpoints/payment-attributes.md
- HighRadius: https://www.highradius.com/?p=219308 · Tesorio: https://www.tesorio.com/blog/how-to-automate-ap-forecasting-and-forecast-cash-flow-in-real-time
- Brex credit limits: https://brex.com/support/brex-credit-limits · Kognitos 2026 treasury tools: https://www.kognitos.com/blog/best-ai-tools-treasury-liquidity-management-2026/
