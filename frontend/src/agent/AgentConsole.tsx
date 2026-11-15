import { useCallback, useEffect, useRef, useState } from 'react'
import { api, usd } from '../api'
import type { AgentPreference, AgentRun, AgentRunSummary, AgentStep, AgentStatus } from '../types'

const USERS: Record<string, string> = { u_maya: 'Maya (finance lead)' }
const ACTIVE: AgentStatus[] = ['queued', 'running']

const STATUS: Record<AgentStatus, { label: string; cls: string }> = {
  queued: { label: 'Queued', cls: 'bg-slate-100 text-slate-700 border-slate-300' },
  running: { label: 'Working', cls: 'bg-cg-accentSoft text-cg-accent border-blue-300' },
  awaiting_approval: { label: 'Awaiting approval', cls: 'bg-warn-soft text-warn-ink border-warn-line' },
  executed: { label: 'Approved and executed', cls: 'bg-allow-soft text-allow-ink border-allow-line' },
  rejected: { label: 'Rejected', cls: 'bg-block-soft text-block-ink border-block-line' },
  failed: { label: 'Failed', cls: 'bg-block-soft text-block-ink border-block-line' },
  superseded: { label: 'Superseded', cls: 'bg-slate-100 text-slate-600 border-slate-300' },
}

const AGENT_CHIP: Record<string, string> = {
  buffer: 'bg-cg-ink text-white',
  'cash-analyst': 'bg-violet-100 text-violet-800',
  'payables-planner': 'bg-amber-100 text-amber-900',
  'funding-planner': 'bg-emerald-100 text-emerald-900',
}

const money = (n: unknown) => typeof n === 'number'
  ? '$' + n.toLocaleString('en-US', { maximumFractionDigits: n % 1 ? 2 : 0 }) : '—'
const fmtDate = (iso: string) => new Date(iso + (iso.length === 10 ? 'T12:00:00' : '')).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })

/** One readable line per tool call. Every figure shown here is the tool's own output. */
function stepLine(s: AgentStep): string {
  const r = s.result as Record<string, any>
  if (s.tool === 'task') return `Delegated to ${s.args.subagent}: ${String(s.args.description ?? '').slice(0, 140)}`
  if (r?.error) return `${r.error}${r.problems ? ': ' + r.problems.join('; ') : ''}`
  switch (s.tool) {
    case 'get_cash_position':
      return `Read ${r.accounts?.length ?? 0} accounts, ${r.expected_receipts?.length ?? 0} expected receipts, ` +
        `${r.stress_set?.scenarios?.length ?? 0} stress scenarios`
    case 'list_open_bills': {
      const bills = (r.bills ?? []) as any[]
      return `${bills.length} open bills; ${bills.filter(b => b.needs_scheduling).length} need scheduling, ` +
        `${bills.filter(b => b.early_pay_discount).length} offer early-pay discounts`
    }
    case 'get_customer_history':
      return (r.customers ?? []).map((c: any) => `${c.customer}: ${c.paid_invoices} paid, ` +
        `${c.open_invoices?.length ?? 0} open, late odds ${JSON.stringify(c.delay_distribution)}`).join(' · ')
    case 'run_risk_search': {
      const f = r.findings?.[0]
      return f ? `Worst of ${r.evaluations} shocks (${r.focus}): "${f.label}" → ${money(f.worst_shortfall_usd)} short`
        : `${r.evaluations} shocks tried (${r.focus}); none breach`
    }
    case 'check_payment_run':
      if (r.verified === false) return `Not safe (${r.status}): ${(r.why ?? []).slice(0, 1).join('')}`
      return `${r.draft_id}: verified on ${r.scenarios_checked} scenarios · discounts ${money(r.discounts_captured_usd)} − ` +
        `funding ${money(r.funding_cost_usd)} = net ${money(r.net_benefit_usd)}`
    case 'read_preferences':
      return r.preferences?.length ? `${r.preferences.length} learned preference(s): ` +
        r.preferences.map((p: any) => `"${p.text}"`).join(', ') : 'No learned preferences yet'
    case 'submit_proposal':
      return `Submitted ${r.submitted} for approval`
    default:
      return JSON.stringify(r).slice(0, 140)
  }
}

export default function AgentConsole({ onExecuted, resetKey }: { onExecuted: () => void; resetKey: number }) {
  const [run, setRun] = useState<AgentRun | null>(null)
  const [history, setHistory] = useState<AgentRunSummary[]>([])
  const [prefs, setPrefs] = useState<AgentPreference[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [reason, setReason] = useState('')
  const [note, setNote] = useState('')
  const [now, setNow] = useState(Date.now())
  const [watchMsg, setWatchMsg] = useState<string | null>(null)
  const polling = useRef<string | null>(null)

  const loadSide = useCallback(async () => {
    try {
      const [h, p] = await Promise.all([api.agentRuns(true), api.agentPreferences()])
      setHistory(h); setPrefs(p)
    } catch { /* side panels are best-effort */ }
  }, [])

  const follow = useCallback(async (id: string) => {
    polling.current = id
    let r = await api.agentRun(id)
    setRun(r)
    while (ACTIVE.includes(r.status) && polling.current === id) {
      await new Promise(res => setTimeout(res, 1000))
      if (polling.current !== id) return
      r = await api.agentRun(id, true)
      setRun(r)
    }
    void loadSide()
  }, [loadSide])

  useEffect(() => {
    setRun(null); setErr(null); polling.current = null
    void (async () => {
      await loadSide()
      try {
        const h = await api.agentRuns(true)
        if (h[0]) void follow(h[0].id)
      } catch { /* no runs yet */ }
    })()
    return () => { polling.current = null }
  }, [resetKey, follow, loadSide])

  useEffect(() => {
    if (!run || !ACTIVE.includes(run.status)) return
    const t = setInterval(() => setNow(Date.now()), 500)
    return () => clearInterval(t)
  }, [run])

  async function act(label: string, fn: () => Promise<void>) {
    setBusy(label); setErr(null)
    try { await fn() } catch (e) { setErr(e instanceof Error ? e.message : String(e)) } finally { setBusy(null) }
  }

  const start = () => act('start', async () => {
    const { run_id } = await api.agentStart(note.trim() || undefined)
    setNote('')
    void follow(run_id)
  })
  const approve = () => act('approve', async () => {
    if (!run) return
    setRun(await api.agentApprove(run.id))
    onExecuted(); void loadSide()
  })
  const reject = (why: string) => act('reject', async () => {
    if (!run) return
    const out = await api.agentReject(run.id, why)
    setReason('')
    if (out.revision_run_id) void follow(out.revision_run_id)
    void loadSide()
  })
  const forget = () => act('forget', async () => { await api.agentForget(); void loadSide() })
  const wake = (fn: () => Promise<{ run_id: string | null; new_alerts: string[] }>, label: string) => act(label, async () => {
    const out = await fn()
    setWatchMsg(out.run_id && out.new_alerts.length
      ? `Buffer noticed ${out.new_alerts.length} change(s) and started working.`
      : 'Nothing new since the last run. The agent stays asleep.')
    if (out.run_id && out.new_alerts.length) { onExecuted(); void follow(out.run_id) }
  })

  const p = run?.proposal
  const active = run && ACTIVE.includes(run.status)
  const elapsed = run ? Math.max(0, Math.round(((run.finished_at ? Date.parse(run.finished_at) : now) - Date.parse(run.started_at)) / 1000)) : 0
  const steps = run?.steps ?? []

  return (
    <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1.45fr)_minmax(0,1fr)]">
      <div className="space-y-4">
        {/* ---- control */}
        <section className="card p-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="max-w-xl">
              <h2 className="text-lg font-semibold text-slate-900">Buffer Agent: this week's payment run</h2>
              <p className="mt-1 text-sm text-slate-600">
                A LangChain deepagent plans, delegates to three subagents, and tests pay-date and funding options
                against every stress scenario. It proposes the plan with the best verified net benefit and waits
                for a finance lead. The language model chooses what to try. The engine computes every number.
              </p>
            </div>
            {run && (
              <span className={`rounded-full border px-3 py-1 text-xs font-semibold ${STATUS[run.status].cls}`}>
                {STATUS[run.status].label}{active ? ` · ${elapsed}s` : ''}
              </span>
            )}
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            <input value={note} onChange={e => setNote(e.target.value)} disabled={!!active || busy !== null}
              placeholder="Optional instruction, e.g. prioritise supplier discounts"
              className="min-w-[16rem] flex-1 rounded-md border border-slate-300 px-3 py-2 text-sm" />
            <button onClick={start} disabled={!!active || busy !== null}
              className="rounded-md bg-cg-accent px-4 py-2 text-sm font-semibold text-white hover:bg-blue-700 disabled:opacity-50">
              {active ? 'Agent working…' : busy === 'start' ? 'Starting…' : 'Run payment review'}
            </button>
          </div>
          <div className="mt-4 rounded-lg border border-dashed border-slate-300 p-3">
            <p className="text-xs text-slate-600">
              <b>Buffer is watching.</b> After every bank sync (fixture or Capital One Nessie) it checks for late
              customer payments, new bills, expiring discounts and projected buffer breaches, and starts on its own
              when something changed. Simulate a change:
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              <button onClick={() => wake(() => api.agentDemoEvent('late_payment'), 'late')} disabled={!!active || busy !== null}
                className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50">
                {busy === 'late' ? 'Simulating…' : 'Globex pays 4 days late'}
              </button>
              <button onClick={() => wake(() => api.agentDemoEvent('new_bill'), 'bill')} disabled={!!active || busy !== null}
                className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50">
                {busy === 'bill' ? 'Simulating…' : 'New $35k Northwind bill arrives'}
              </button>
              <button onClick={() => wake(api.agentWatch, 'watch')} disabled={!!active || busy !== null}
                className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50">
                {busy === 'watch' ? 'Checking…' : 'Check for changes now'}
              </button>
            </div>
            {watchMsg && <p className="mt-2 text-xs text-slate-600" role="status">{watchMsg}</p>}
          </div>
          {run && run.trigger.length > 0 && (
            <p className="mt-3 text-xs text-slate-500">
              <span className="font-semibold text-slate-600">Triggered by:</span> {run.trigger.join(' · ')}
              {run.model && <> · {run.model}, {run.llm_calls} model turns, {steps.length} tool steps</>}
            </p>
          )}
          {err && <p className="mt-3 rounded-md border border-block-line bg-block-soft p-2 text-sm text-block-ink">{err}</p>}
          {run?.status === 'failed' && (
            <p className="mt-3 rounded-md border border-block-line bg-block-soft p-2 text-sm text-block-ink">
              The agent could not finish: {run.error}
            </p>
          )}
        </section>

        {/* ---- proposal */}
        {p && (
          <section className="card p-5" aria-label="Proposal">
            <div className="flex flex-wrap items-end justify-between gap-4">
              <div>
                <h3 className="text-base font-semibold text-slate-900">Proposed payment run</h3>
                <p className="text-xs text-slate-500">
                  Verified in exact cents against {p.scenarios_checked} scenarios (baseline + stress set)
                  {p.avoid.length > 0 && <> · avoiding {p.avoid.join(', ')}</>}
                </p>
              </div>
              <div className="flex gap-6 text-right">
                <Stat label="Discounts captured" value={usd(p.discounts_cents)} />
                <Stat label="Funding cost" value={usd(p.cost_cents)} />
                <Stat label="Net benefit" value={usd(p.net_benefit_cents)} strong />
              </div>
            </div>

            <div className="mt-4 overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
                  <tr><th className="py-1.5 pr-3">Bill</th><th className="pr-3">Payee</th><th className="pr-3 text-right">Amount</th>
                    <th className="pr-3">Pay on</th><th>Due</th></tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {p.bills.map(b => (
                    <tr key={b.bill_id}>
                      <td className="py-1.5 pr-3 font-mono text-xs">{b.bill_id}</td>
                      <td className="pr-3">{b.payee}</td>
                      <td className="pr-3 text-right tabular-nums">{usd(b.amount_cents)}</td>
                      <td className="pr-3">
                        <span className={b.moved ? 'font-semibold text-allow-ink' : ''}>{fmtDate(b.pay_date)}</span>
                        {b.moved && <span className="ml-2 rounded bg-allow-soft px-1.5 py-0.5 text-2xs font-semibold text-allow-ink">early: discount</span>}
                      </td>
                      <td className="text-slate-500">{fmtDate(b.due_date)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <div className="rounded-lg border border-slate-200 p-3">
                <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Funding</p>
                {p.funding.length === 0 ? <p className="mt-1 text-sm text-slate-600">None needed.</p> : (
                  <ul className="mt-1 space-y-1 text-sm">
                    {p.funding.map((f, i) => (
                      <li key={i}>
                        {f.kind === 'transfer'
                          ? <>Transfer <b>{usd(f.amount_cents)}</b> {f.from_account} → {f.to_account} on {fmtDate(f.dispatch_date!)}</>
                          : <>Draw <b>{usd(f.amount_cents)}</b> on {f.line_id} on {fmtDate(f.date!)}</>}
                        <span className="text-slate-500"> · fee {usd(f.fee_cents)}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              <div className="rounded-lg border border-slate-200 p-3">
                <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Held back</p>
                {p.held_bills.length === 0 ? <p className="mt-1 text-sm text-slate-600">Nothing.</p> : (
                  <ul className="mt-1 space-y-1 text-sm">
                    {p.held_bills.map(h => <li key={h.bill_id}><span className="font-mono text-xs">{h.bill_id}</span>: {h.reason}</li>)}
                  </ul>
                )}
              </div>
            </div>

            <div className="mt-4 rounded-lg bg-slate-50 p-3 text-sm leading-6 text-slate-700">
              <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Agent's reasoning</p>
              {p.rationale}
            </div>

            {run?.status === 'awaiting_approval' && (
              <div className="mt-4 border-t border-slate-200 pt-4">
                <p className="mb-2 text-sm text-slate-600">Nothing has moved yet. Approving as <b>{USERS.u_maya}</b> schedules
                  these bills and executes the funding{run && ' (Nessie sandbox when writes are enabled)'}.</p>
                <div className="flex flex-wrap items-center gap-2">
                  <button onClick={approve} disabled={busy !== null}
                    className="rounded-md bg-allow px-4 py-2 text-sm font-semibold text-white hover:opacity-90 disabled:opacity-50">
                    {busy === 'approve' ? 'Executing…' : 'Approve & execute'}
                  </button>
                  <input value={reason} onChange={e => setReason(e.target.value)} disabled={busy !== null}
                    placeholder="Reason for rejecting (the agent learns it)"
                    className="min-w-[14rem] flex-1 rounded-md border border-slate-300 px-3 py-2 text-sm" />
                  <button onClick={() => reject(reason)} disabled={busy !== null || !reason.trim()}
                    className="rounded-md border border-block-line bg-white px-4 py-2 text-sm font-semibold text-block-ink hover:bg-block-soft disabled:opacity-50">
                    {busy === 'reject' ? 'Rejecting…' : 'Reject & revise'}
                  </button>
                </div>
                <div className="mt-2 flex flex-wrap gap-2 text-xs">
                  {["Don't touch the strategic reserve this week", 'No credit line draws'].map(s => (
                    <button key={s} onClick={() => setReason(s)} disabled={busy !== null}
                      className="rounded-full border border-slate-300 px-2.5 py-1 text-slate-600 hover:bg-slate-50">{s}</button>
                  ))}
                </div>
              </div>
            )}
            {run?.status === 'rejected' && (
              <p className="mt-4 rounded-md border border-block-line bg-block-soft p-2 text-sm text-block-ink">
                Rejected: "{run.rejection_reason}". The agent saved this as a preference and is revising.
              </p>
            )}
          </section>
        )}

        {/* ---- execution */}
        {run?.execution && (
          <section className="card border-allow-line p-5" aria-label="Execution">
            <h3 className="text-base font-semibold text-allow-ink">Executed: {run.execution.label}</h3>
            <p className="mt-1 text-sm text-slate-600">
              Approved by {USERS[run.execution.approved_by] ?? run.execution.approved_by}. Snapshot
              {' '}<span className="font-mono text-xs">{run.execution.snapshot_before}</span> →
              {' '}<span className="font-mono text-xs">{run.execution.snapshot_after}</span>
            </p>
            {run.execution.provider_refs.length > 0 && (
              <ul className="mt-2 space-y-0.5 text-xs text-slate-600">
                {run.execution.provider_refs.map(r => (
                  <li key={r.object_id}><b>Nessie {r.object_type}</b> <span className="font-mono">{r.object_id}</span> {r.description}</li>
                ))}
              </ul>
            )}
            {run.execution.reconciliation && (
              <p className={`mt-2 text-sm ${run.execution.reconciliation.balanced ? 'text-allow-ink' : 'text-block-ink'}`}>
                Reconciliation after re-sync: {run.execution.reconciliation.balanced ? 'balanced' : 'differences found'}
                {' '}({usd(run.execution.reconciliation.unexplained_cents)} unexplained, {usd(run.execution.reconciliation.in_flight_cents)} in flight)
              </p>
            )}
            <ul className="mt-2 list-disc pl-5 text-xs text-slate-500">
              {run.execution.notes.map((n, i) => <li key={i}>{n}</li>)}
            </ul>
          </section>
        )}
      </div>

      {/* ---- right column: plan, trace, memory */}
      <div className="space-y-4">
        <section className="card p-4" aria-label="Plan">
          <h3 className="text-sm font-semibold text-slate-900">Agent plan</h3>
          {(!run || run.todos.length === 0) ? (
            <p className="mt-2 text-sm text-slate-500">{active ? 'Writing its plan…' : 'Run the agent to see its plan.'}</p>
          ) : (
            <ul className="mt-2 space-y-1.5 text-sm">
              {run.todos.map((t, i) => (
                <li key={i} className="flex items-start gap-2">
                  <span aria-hidden className={`mt-0.5 inline-flex h-4 w-4 flex-none items-center justify-center rounded-full border text-2xs
                    ${t.status === 'completed' ? 'border-allow bg-allow text-white' : t.status === 'in_progress' ? 'animate-pulse border-cg-accent bg-cg-accentSoft' : 'border-slate-300'}`}>
                    {t.status === 'completed' ? '✓' : ''}
                  </span>
                  <span className={t.status === 'completed' ? 'text-slate-500' : 'text-slate-800'}>{t.content}</span>
                  <span className="sr-only">({t.status})</span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="card p-4" aria-label="Activity">
          <h3 className="text-sm font-semibold text-slate-900">Activity <span className="font-normal text-slate-500">({steps.length} steps)</span></h3>
          <ol className="mt-2 max-h-[30rem] space-y-2 overflow-y-auto pr-1 text-sm" aria-live="polite">
            {steps.length === 0 && <li className="text-slate-500">No steps yet.</li>}
            {steps.map(s => (
              <li key={s.step} className="rounded-md border border-slate-100 p-2">
                <div className="flex flex-wrap items-center gap-1.5 text-2xs">
                  <span className={`rounded px-1.5 py-0.5 font-semibold ${AGENT_CHIP[s.agent] ?? 'bg-slate-100 text-slate-700'}`}>{s.agent}</span>
                  <span className="font-mono text-slate-500">{s.tool}</span>
                  {s.ms > 0 && <span className="text-slate-400">{s.ms} ms</span>}
                </div>
                <p className="mt-1 text-xs leading-5 text-slate-700">{stepLine(s)}</p>
              </li>
            ))}
            {active && <li className="animate-pulse text-xs text-slate-500">Thinking…</li>}
          </ol>
        </section>

        <section className="card p-4" aria-label="Learned preferences">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-slate-900">Learned preferences</h3>
            {prefs.length > 0 && <button onClick={forget} disabled={busy !== null} className="text-xs text-slate-500 underline hover:text-slate-800">Forget all</button>}
          </div>
          {prefs.length === 0 ? <p className="mt-2 text-sm text-slate-500">None yet. Reject a proposal with a reason to teach the agent.</p> : (
            <ul className="mt-2 space-y-1.5 text-sm">
              {prefs.map(pr => (
                <li key={pr.id}>"{pr.text}"
                  {pr.flags.length > 0 && <span className="ml-1 text-2xs text-slate-500">enforced: {pr.flags.join(', ')}</span>}
                </li>
              ))}
            </ul>
          )}
        </section>

        {history.length > 1 && (
          <section className="card p-4" aria-label="Run history">
            <h3 className="text-sm font-semibold text-slate-900">Runs</h3>
            <ul className="mt-2 space-y-1 text-xs">
              {history.slice(0, 8).map(h => (
                <li key={h.id}>
                  <button onClick={() => void follow(h.id)} className={`w-full rounded px-2 py-1 text-left hover:bg-slate-50 ${run?.id === h.id ? 'bg-slate-100' : ''}`}>
                    <span className="font-mono">{h.id}</span> · {STATUS[h.status].label}
                    {h.net_benefit_cents != null && <> · net {usd(h.net_benefit_cents)}</>}
                    {h.parent_id && <span className="text-slate-500"> · revision</span>}
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </div>
  )
}

function Stat({ label, value, strong }: { label: string; value: string; strong?: boolean }) {
  return (
    <div>
      <p className="text-2xs uppercase tracking-wide text-slate-500">{label}</p>
      <p className={`tabular-nums ${strong ? 'text-xl font-bold text-allow-ink' : 'text-lg font-semibold text-slate-800'}`}>{value}</p>
    </div>
  )
}
