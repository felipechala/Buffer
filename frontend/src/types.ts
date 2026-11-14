// Mirrors backend/app/api/v1.py, services/decision.py, services/fixes.py (all money in integer cents).
export type DecisionLevel = 'allow' | 'warn' | 'block'

export interface Scenario {
  id: string
  description: string
  receipt_delays: Record<string, number>
  receipt_haircuts: Record<string, number>
  unexpected_outflows: unknown[]
  disabled_routes: string[]
  disabled_credit_lines: string[]
  seed: number | null
}

export interface BreachPoint { account_id: string; day: number; shortfall_cents: number }
export interface SimMetrics {
  breach: boolean
  first_breach: BreachPoint | null
  worst_shortfall_cents: number
  shortfall_cent_days: number
  uncovered_obligations: string[]
  uncovered_mandatory_cents: number
  fees_cents: number
  min_headroom_cents: number
}

export interface Violation {
  scenario_id: string
  account_id: string
  day: number
  shortfall_cents: number
  explanations: string[]
}

export interface ConnectionHealth {
  provider: 'fixture' | 'nessie'
  status: 'healthy' | 'configured' | 'unavailable' | 'error' | 'not_configured'
  scope: string
  last_sync_at: string | null
  detail: string
}
export interface ConnectionsResponse {
  mode: 'fixture' | 'nessie'
  connections: ConnectionHealth[]
  snapshot_id: string | null
  writes_enabled: boolean
}
export interface SyncResponse {
  sync_id: string
  provider: string
  snapshot_id: string
  source_count: Record<string, number>
  synced_at: string
  stress_run_id: string | null
  warnings: string[]
}

export interface AccountState {
  id: string; name: string; role: string
  observed_balance_cents: number; buffer_cents: number
  provider: string; provider_id: string | null; as_of: string
}
export interface ObligationState {
  id: string; account_id: string; amount_cents: number; due_day: number; due_date: string
  counterparty: string; category: string; nondeferrable: boolean
}
export interface ReceiptState {
  id: string; account_id: string; amount_cents: number; expected_day: number; expected_date: string
  counterparty: string; delay_distribution: Record<string, number>
}
export interface TwinStateResponse {
  snapshot_id: string; company: string; as_of: string; horizon_days: number
  accounts: AccountState[]; obligations: ObligationState[]; receipts: ReceiptState[]
  routes: Record<string, unknown>[]; credit_lines: Record<string, unknown>[]
  provenance: Record<string, string>; assumptions: string[]
}

export interface AccountForecast {
  account_id: string; buffer_cents: number; dates: string[]
  baseline: number[]; p10: number[]; p50: number[]; p90: number[]
  worst_stress: number[]; worst_stress_id: string
}
export interface ForecastResponse {
  snapshot_id: string; horizon_days: number; accounts: AccountForecast[]
  baseline_metrics: SimMetrics; stress_set_id: string; provenance: Record<string, unknown>
}

export interface ProposedPayment {
  kind?: 'payment'
  bill_id?: string | null
  amount_cents?: number | null
  from_account?: string | null
  payee?: string | null
  pay_date?: string | null
  latest_date?: string | null
  priority?: 'low' | 'normal' | 'high' | 'critical'
}
export interface SimulationRequest {
  snapshot_id?: string | null
  proposed_action?: ProposedPayment | null
  scenario?: Partial<Scenario> | null
  horizon_days?: number | null
  risk_tolerance?: number | null
  scenario_count?: number
  seed?: number
}

export interface ScenarioRef { id: string; label: string; plausibility: number }
export interface Break {
  date: string; day: number; account: string; obligation: string | null
  shortfall_cents: number; scenario: ScenarioRef
}
export type FixAction =
  | { kind: 'schedule_bill'; bill_id: string; amount_cents: number; pay_day: number; pay_date: string }
  | { kind: 'transfer'; route_id: string; from_account: string; to_account: string; amount_cents: number
      dispatch_day: number; dispatch_date: string; arrival_day: number; fee_cents: number }
  | { kind: 'credit_draw'; line_id: string; account: string; amount_cents: number; day: number
      date: string; funds_day: number; fee_cents: number }
export interface StressCheck {
  scenario_id: string; breach: boolean; worst_shortfall_cents: number
  min_headroom_cents: number; rejected_actions: number
}
export interface Fix {
  id: string; bill_id: string; snapshot_id: string; run_id: string
  kind: 'reschedule' | 'funding' | 'reschedule+funding'
  summary: string; pay_day: number; pay_date: string; delay_days: number; cost_cents: number
  actions: FixAction[]; verified: boolean
  held_out: { breach_rate: number; paths: number }
  stress_checks: StressCheck[]
  plan_actions: unknown[]
}
export interface Decision {
  run_id: string
  decision: DecisionLevel
  safe: boolean
  bill_id: string; payee: string; amount_cents: number
  requested_day: number; requested_date: string
  breaks: Break[]; fixes: Fix[]; infeasibility: Violation[]
  curves: Record<string, Record<string, number[]>>
  constraint_report: string[]
  explanation: string
  impact: Record<string, unknown>
  provenance: {
    snapshot_id: string; input_digest: string; engine_version: string; scenario_seed: number
    stress_set_id: string; stress_set_source: string; scenarios_checked: number; source: string
    assumptions: string[]
  }
  latency_ms: number
}
export interface CounterfactualResponse {
  snapshot_id: string
  scenario: Scenario
  before: Record<string, number[]>
  after: Record<string, number[]>
  before_metrics: SimMetrics
  after_metrics: SimMetrics
  constraint_report: string[]
  provenance: Record<string, unknown>
}
export interface StressRunResponse {
  run_id: string; snapshot_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  source: string
  scenarios: Scenario[]
  findings: { scenario_id: string; label: string; breach: boolean; worst_shortfall_cents: number
              first_breach: BreachPoint | null }[]
  trace: Record<string, unknown>[]
  provenance: Record<string, unknown>
}
export interface Approval {
  id: string; subject: string; subject_id: string; approver_id: string; role: string
  decision: string; timestamp: string; memo: string
}
export interface Execution {
  id: string; fix_id: string; run_id: string; approved_by: string
  state: 'simulated' | 'confirmed' | 'uncertain'; mode: 'simulated' | 'sandbox'
  executed_at: string; cash_event_ids: string[]
  provider_refs: { provider: string; object_type: string; object_id: string; description: string }[]
  snapshot_before: string; snapshot_after: string; notes: string[]
}
export interface AuditEvent {
  seq: number | null; timestamp: string; actor: string; action: string
  entity: string; entity_id: string; correlation_id: string
  before: Record<string, unknown> | null; after: Record<string, unknown> | null
}
