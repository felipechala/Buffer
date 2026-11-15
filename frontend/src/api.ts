import type {
  Approval, AuditEvent, ConnectionsResponse, CounterfactualResponse, Decision, Execution,
  AgentPreference, AgentRun, AgentRunSummary,
  CommitResponse, ExplanationResponse, ForecastResponse, PurchaseRequest, SimulationRequest, StressRunResponse, SyncResponse, TwinStateResponse,
} from './types'

const API_KEY: string = import.meta.env.VITE_CASHGUARD_API_KEY || 'cg_test_demo_key'

export interface CallRecord {
  id: number
  method: string
  path: string
  headers: Record<string, string>
  request: unknown
  response: unknown
  status: number
  ms: number
  at: string
}

type Recorder = (r: Omit<CallRecord, 'id'>) => void
let recorder: Recorder = () => {}
export function setRecorder(fn: Recorder) { recorder = fn }

export class ApiError extends Error {
  status: number
  body: unknown
  constructor(status: number, body: unknown, msg: string) { super(msg); this.status = status; this.body = body }
}

export async function call<T>(method: string, path: string, body?: unknown,
                              extraHeaders: Record<string, string> = {}, record = true): Promise<T> {
  const headers: Record<string, string> = { Authorization: `Bearer ${API_KEY}`, ...extraHeaders }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const t0 = performance.now()
  let status = 0
  let parsed: unknown = null
  try {
    const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) })
    status = res.status
    const text = await res.text()
    try { parsed = text ? JSON.parse(text) : null } catch { parsed = text }
    if (!res.ok) {
      const msg = (parsed as { error?: { message?: string } })?.error?.message ?? `HTTP ${res.status}`
      throw new ApiError(res.status, parsed, msg)
    }
    return parsed as T
  } catch (e) {
    if (!(e instanceof ApiError)) parsed = { error: String(e) }
    throw e
  } finally {
    if (record) recorder({ method, path, headers, request: body ?? null, response: parsed, status,
               ms: Math.round(performance.now() - t0), at: new Date().toISOString() })
  }
}

export function idemKey(prefix: string) {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

export const api = {
  connections: () => call<ConnectionsResponse>('GET', '/v1/connections'),
  syncFixture: (reset = false) =>
    call<SyncResponse>('POST', `/v1/connections/fixture/sync${reset ? '?reset=true' : ''}`),
  syncNessie: () => call<SyncResponse>('POST', '/v1/connections/nessie/sync'),
  explanation: (url: string) => call<ExplanationResponse>('GET', url),
  state: () => call<TwinStateResponse>('GET', '/v1/twin/state'),
  forecast: (body: { scenario_count?: number; seed?: number } = {}) =>
    call<ForecastResponse>('POST', '/v1/twin/forecasts', body),
  simulate: (body: SimulationRequest) => call<Decision>('POST', '/v1/twin/simulations', body),
  counterfactual: (body: SimulationRequest) =>
    call<CounterfactualResponse>('POST', '/v1/twin/simulations', body),
  startStress: (mode: 'deterministic' | 'deepagent' = 'deterministic') =>
    call<{ run_id: string; status: string; snapshot_id: string }>('POST', '/v1/twin/stress-tests', { mode }),
  getStress: (id: string, waitS = 4) =>
    call<StressRunResponse>('GET', `/v1/twin/stress-tests/${encodeURIComponent(id)}?wait_s=${waitS}`),
  approve: (fixId: string, approvedBy: string, memo = '') =>
    call<Approval>('POST', `/v1/plans/${encodeURIComponent(fixId)}/approve`,
                   { approved_by: approvedBy, memo }, { 'Idempotency-Key': idemKey('approve') }),
  execute: (fixId: string, key: string) =>
    call<Execution>('POST', `/v1/plans/${encodeURIComponent(fixId)}/execute-sandbox`, undefined,
                    { 'Idempotency-Key': key }),
  purchasesDemo: () => call<{ requests: PurchaseRequest[] }>('GET', '/v1/purchases/demo'),
  commitPurchase: (body: { run_id: string; fix_id: string | null; pay_date: string | null }) =>
    call<CommitResponse>('POST', '/v1/purchases/commit', body, { 'Idempotency-Key': idemKey('commit') }),
  events: (limit = 200) => call<AuditEvent[]>('GET', `/v1/events?limit=${limit}`),
  agentStart: (note?: string) => call<{ run_id: string; status: string }>('POST', '/v1/agent/runs', note ? { note } : {}),
  agentRuns: (quiet = false) => call<AgentRunSummary[]>('GET', '/v1/agent/runs', undefined, {}, !quiet),
  agentRun: (id: string, quiet = false) =>
    call<AgentRun>('GET', `/v1/agent/runs/${encodeURIComponent(id)}`, undefined, {}, !quiet),
  agentApprove: (id: string, approvedBy = 'u_maya') =>
    call<AgentRun>('POST', `/v1/agent/runs/${encodeURIComponent(id)}/approve`, { approved_by: approvedBy }),
  agentReject: (id: string, reason: string, rejectedBy = 'u_maya') =>
    call<{ run_id: string; revision_run_id: string | null; preference: AgentPreference | null }>(
      'POST', `/v1/agent/runs/${encodeURIComponent(id)}/reject`, { rejected_by: rejectedBy, reason }),
  agentPreferences: () => call<AgentPreference[]>('GET', '/v1/agent/preferences'),
  agentForget: () => call<{ deleted: number }>('DELETE', '/v1/agent/preferences'),
}

export const usd = (cents: number) =>
  (cents < 0 ? '-' : '') + '$' + Math.abs(cents / 100).toLocaleString('en-US', { maximumFractionDigits: 0 })

// ---- copy-as helpers
const BASE = 'http://127.0.0.1:8000'
export function toCurl(r: CallRecord) {
  const h = Object.entries(r.headers).map(([k, v]) => `  -H "${k}: ${v}"`).join(' \\\n')
  const body = r.request != null ? ` \\\n  -d '${JSON.stringify(r.request)}'` : ''
  return `curl -X ${r.method} "${BASE}${r.path}" \\\n${h}${body}`
}
export function toPython(r: CallRecord) {
  return `import requests\n\nres = requests.request(\n    "${r.method}", "${BASE}${r.path}",\n` +
    `    headers=${JSON.stringify(r.headers, null, 4)},\n` +
    (r.request != null ? `    json=${JSON.stringify(r.request, null, 4)},\n` : '') +
    `)\nprint(res.status_code, res.json())`
}
export function toJs(r: CallRecord) {
  return `const res = await fetch("${BASE}${r.path}", {\n  method: "${r.method}",\n` +
    `  headers: ${JSON.stringify(r.headers, null, 2)},\n` +
    (r.request != null ? `  body: JSON.stringify(${JSON.stringify(r.request, null, 2)}),\n` : '') +
    `});\nconsole.log(res.status, await res.json());`
}

export const prettyId = (s: string) =>
  s.replace(/[_-]+/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
