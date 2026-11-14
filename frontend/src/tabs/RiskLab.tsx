import { useEffect, useMemo, useRef, useState } from 'react'
import { api, prettyId, usd } from '../api'
import type { CounterfactualResponse, Decision, StressRunResponse, TwinStateResponse } from '../types'
import { groupBreaks, range } from '../components/ResultCard'
import SearchCompare from '../components/SearchCompare'
import EvalPanel from '../components/EvalPanel'

const BADGE = {
  allow: 'bg-emerald-600 text-white',
  warn: 'bg-amber-500 text-white',
  block: 'bg-red-600 text-white',
} as const
const ICON = { allow: '✓', warn: '!', block: '✕' } as const

const isPosted = (r: { id: string; posted?: boolean }) => r.posted === true || r.id.startsWith('posted:')

export default function RiskLabTab({ state, rev }: { state: TwinStateResponse | null; rev: number }) {
  const [receiptId, setReceiptId] = useState('')
  const [delay, setDelay] = useState(2)
  const [amount, setAmount] = useState(40000)
  const [cf, setCf] = useState<CounterfactualResponse | null>(null)
  const [decision, setDecision] = useState<Decision | null>(null)
  const [stress, setStress] = useState<StressRunResponse | null>(null)
  const [mode, setMode] = useState<'deterministic' | 'deepagent'>('deterministic')
  const [busy, setBusy] = useState(false)
  const [stressBusy, setStressBusy] = useState<{ mode: string; started: number } | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const [err, setErr] = useState<string | null>(null)
  const [stressErr, setStressErr] = useState<string | null>(null)
  const runToken = useRef(0)

  const receipts = useMemo(() => (state?.receipts ?? []).filter(r => !isPosted(r)), [state])

  useEffect(() => {
    if (state && !receipts.some(r => r.id === receiptId)) {
      setReceiptId(receipts.find(r => r.id === 'INV-4471')?.id ?? receipts[0]?.id ?? '')
    }
  }, [state, receipts, receiptId])
  useEffect(() => { setCf(null); setDecision(null); setStress(null); runToken.current++ }, [rev])
  useEffect(() => {
    if (!stressBusy) return
    const t = setInterval(() => setElapsed(Math.round((Date.now() - stressBusy.started) / 1000)), 500)
    return () => clearInterval(t)
  }, [stressBusy])

  async function run() {
    setBusy(true); setErr(null)
    try {
      const scenario = { id: 'judge-lab', description: `Receipt ${receiptId} delayed ${delay}d`,
                         receipt_delays: receiptId ? { [receiptId]: delay } : {} }
      // 1) the shock alone (no payment): constraint report + curves
      setCf(await api.counterfactual({ scenario }))
      // 2) a what-if payment under the same shock, via the decision endpoint
      setDecision(await api.simulate({ proposed_action: { kind: 'payment', amount_cents: amount * 100,
        from_account: state?.accounts[0]?.id ?? 'operating', payee: 'Northwind' }, scenario }))
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }

  async function runStress() {
    const token = ++runToken.current
    setStressBusy({ mode, started: Date.now() }); setElapsed(0); setStressErr(null); setStress(null)
    try {
      const { run_id } = await api.startStress(mode)
      let r = await api.getStress(run_id)
      const deadline = Date.now() + 5 * 60_000
      while ((r.status === 'queued' || r.status === 'running') && Date.now() < deadline) {
        if (token !== runToken.current) return
        r = await api.getStress(run_id)
      }
      if (token !== runToken.current) return
      if (r.status === 'failed') setStressErr(r.error ?? 'The stress run failed.')
      else if (r.status !== 'completed') setStressErr(`Still ${r.status} after 5 minutes; try again.`)
      setStress(r)
    } catch (e) { setStressErr(e instanceof Error ? e.message : String(e)) } finally {
      if (token === runToken.current) setStressBusy(null)
    }
  }

  // tightest point of the no-payment projection relative to each account's buffer
  const tightest = useMemo(() => {
    if (!cf || !state) return null
    let best: { acct: string; headroom: number } | null = null
    const shocked = receipts.find(r => r.id === receiptId)?.account_id
    for (const a of state.accounts.filter(a => a.id === shocked)) {
      const curve = cf.after[a.id]
      if (!curve?.length) continue
      const h = Math.min(...curve) - a.buffer_cents
      if (!best || h < best.headroom) best = { acct: a.id, headroom: h }
    }
    return best
  }, [cf, state, receipts, receiptId])

  const groups = decision ? groupBreaks(decision.breaks) : []

  return (
    <div className="grid gap-6 text-sm lg:grid-cols-2">
      <div className="space-y-3">
        <h3 className="font-semibold">Counterfactual: judge a shock</h3>
        <label className="block">
          <span className="text-xs text-slate-500">Delayed receivable</span>
          <select value={receiptId} onChange={e => setReceiptId(e.target.value)} className="mt-1 block w-full rounded border border-slate-300 p-1">
            {receipts.map(r => <option key={r.id} value={r.id}>{r.id} · {prettyId(r.counterparty)} · {usd(r.amount_cents)}</option>)}
          </select>
        </label>
        <label className="block">
          <span className="text-xs text-slate-500">Receivable delay: <b>{delay} days</b></span>
          <input type="range" min={0} max={10} value={delay} onChange={e => setDelay(+e.target.value)} className="block w-full" />
        </label>
        <label className="block">
          <span className="text-xs text-slate-500">What-if bill amount (USD)</span>
          <input type="number" min={1} step={1000} value={amount} onChange={e => setAmount(Math.max(1, +e.target.value))}
            className="mt-1 block w-40 rounded border border-slate-300 p-1 font-mono" />
        </label>
        <button onClick={run} disabled={busy} className="rounded bg-violet-700 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-50">
          {busy ? 'Running...' : 'Run simulation'}
        </button>
        {err && <div className="rounded bg-red-50 p-2 text-red-700">{err}</div>}

        {decision && (
          <div className="rounded border border-slate-200 p-3" data-testid="whatif">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs font-semibold uppercase text-slate-500">What-if payment</span>
              <span className={`inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs font-bold ${BADGE[decision.decision]}`}>
                <span aria-hidden>{ICON[decision.decision]}</span>{decision.decision.toUpperCase()}
              </span>
              <span className="text-slate-700">{usd(amount * 100)} with {receiptId} {delay} day{delay === 1 ? '' : 's'} late</span>
              <span className="ml-auto font-mono text-xs text-slate-400">{decision.latency_ms} ms</span>
            </div>
            <p className="mt-1 text-slate-700">{decision.explanation}</p>
            {groups.length > 0 && (
              <ul className="mt-1 space-y-0.5">
                {groups.slice(0, 5).map((g, i) => (
                  <li key={i} className="text-slate-700">
                    <span className="font-mono text-xs">{range(g.dates)}</span> {g.first.account} short {usd(g.first.shortfall_cents)}
                    <span className="text-slate-500"> — {g.first.scenario.label}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {cf && (
          <div className="rounded border border-slate-200 bg-slate-50 p-3" data-testid="without-payment">
            <div className="text-xs font-semibold uppercase text-slate-500">Without the payment</div>
            <p className="text-xs text-slate-500">Only the shock, no new bill: does the existing plan keep every account above its buffer?</p>
            {cf.constraint_report.length === 0
              ? <p className="mt-1 text-emerald-700">✓ No buffer breach from the shock alone.</p>
              : <ul className="mt-1 list-disc pl-5 text-slate-700">{cf.constraint_report.map((r, i) => <li key={i}>{r}</li>)}</ul>}
            <div className="mt-2 text-xs text-slate-500">
              {cf.after_metrics.breach
                ? <>worst shortfall {usd(cf.after_metrics.worst_shortfall_cents)} (no shock: {usd(cf.before_metrics.worst_shortfall_cents)})</>
                : tightest
                  ? <>closest approach on {tightest.acct}: {usd(tightest.headroom)} {tightest.headroom >= 0 ? 'above' : 'below'} its buffer</>
                  : <>no breach</>}
            </div>
          </div>
        )}
      </div>

      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="font-semibold">Stress tests</h3>
          <select value={mode} onChange={e => setMode(e.target.value as 'deterministic' | 'deepagent')} disabled={!!stressBusy}
            className="rounded border border-slate-300 px-1 py-0.5 text-xs" aria-label="Stress mode">
            <option value="deterministic">deterministic</option>
            <option value="deepagent">deepagent</option>
          </select>
          <button onClick={runStress} disabled={!!stressBusy} className="rounded border border-slate-300 px-2 py-0.5 text-xs hover:bg-slate-50 disabled:opacity-50">
            {stressBusy ? 'Running...' : 'Run stress test'}
          </button>
        </div>
        {stressBusy && (
          <div className="rounded border border-violet-200 bg-violet-50 p-2 text-xs text-violet-900" role="status">
            <span className="mr-2 inline-block h-2 w-2 animate-pulse rounded-full bg-violet-600" />
            {stressBusy.mode === 'deepagent'
              ? `The agent is probing for shocks (can take 20-60 s)... ${elapsed}s`
              : `Searching for adverse shocks... ${elapsed}s`}
          </div>
        )}
        {stressErr && <div className="rounded bg-red-50 p-2 text-red-700">{stressErr}</div>}
        {stress && stress.status === 'completed' && <StressResult s={stress} />}
        <h3 className="pt-2 font-semibold">Policy evaluation (A / B / B+ / C)</h3>
        <EvalPanel />
      </div>
    </div>
  )
}

function StressResult({ s }: { s: StressRunResponse }) {
  const elites = s.elites ?? []
  return (
    <div className="rounded border border-slate-200 p-3" data-testid="stress-result">
      <div className="text-xs text-slate-500">
        run <span className="font-mono">{s.run_id}</span> · {s.mode ?? 'deterministic'} · source {s.source}
        {s.runtime_ms != null && <> · search {(s.runtime_ms / 1000).toFixed(1)} s</>}
      </div>
      <div className="mt-2 text-xs font-semibold uppercase text-slate-500">Worst shocks found ({elites.length})</div>
      {elites.length === 0 ? (
        s.findings.length === 0
          ? <p className="mt-1 text-slate-600">No breaching shocks found within the search budget.</p>
          : <ul className="mt-1 space-y-1">{s.findings.map(f => (
              <li key={f.scenario_id} className="flex items-center gap-2">
                <span className={`rounded px-1.5 text-xs font-semibold ${f.breach ? 'bg-red-100 text-red-800' : 'bg-emerald-100 text-emerald-800'}`}>{f.breach ? '✕ breach' : '✓ ok'}</span>
                <span className="truncate">{f.label}</span>
                {f.breach && <span className="ml-auto font-mono text-xs">{usd(f.worst_shortfall_cents)}</span>}
              </li>))}</ul>
      ) : (
        <ul className="mt-1 space-y-1.5">
          {elites.slice(0, 8).map(e => (
            <li key={e.scenario.id} className="rounded border border-slate-100 p-1.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-slate-800">{e.scenario.description}</span>
                <span className="text-xs text-slate-500">{(e.plausibility * 100).toFixed(0)}% plausible</span>
                <span className={`ml-auto rounded px-1.5 text-xs font-semibold ${e.fixable ? 'bg-emerald-100 text-emerald-800' : 'bg-red-100 text-red-800'}`}>
                  {e.fixable ? '✓ fixable' : '✕ unfixable'}
                </span>
                <span className="font-mono text-xs">short {usd(e.worst_shortfall_cents)}</span>
              </div>
              {!e.fixable && e.infeasibility.length > 0 && <div className="mt-0.5 text-xs text-slate-500">{e.infeasibility[0]}</div>}
            </li>
          ))}
        </ul>
      )}
      <SearchCompare adaptive={s.adaptive} random={s.random_baseline} />
      {s.mode === 'deepagent' && (
        <div className="mt-3 border-t border-slate-200 pt-2 text-xs">
          <div className="font-semibold text-slate-600">
            Agent: {s.agent}{s.agent === 'fallback' && s.agent_reason ? ` (${s.agent_reason})` : ''}
            {s.agent_meta?.llm_calls != null && <span className="ml-2 font-normal text-slate-400">{s.agent_meta.llm_calls} LLM calls, {s.agent_meta.tool_calls ?? 0} tool calls</span>}
          </div>
          <ol className="mt-1 list-decimal space-y-0.5 pl-4 text-slate-600">
            {(s.narrative ?? []).map(n => (
              <li key={n.step}>
                {n.tool ? <><span className="font-mono">{n.tool}</span> <span className="text-slate-400">{JSON.stringify(n.args)}</span></> : <>{String((n.result as { summary?: string }).summary ?? '')}</>}
                {n.tool && n.result && <div className="truncate text-slate-400">{JSON.stringify(n.result).slice(0, 160)}</div>}
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  )
}
