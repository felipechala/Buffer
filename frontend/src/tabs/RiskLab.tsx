import { useEffect, useState } from 'react'
import { api, usd } from '../api'
import type { CounterfactualResponse, StressRunResponse, TwinStateResponse } from '../types'

export default function RiskLabTab({ state, rev }: { state: TwinStateResponse | null; rev: number }) {
  const [receiptId, setReceiptId] = useState('')
  const [delay, setDelay] = useState(2)
  const [amount, setAmount] = useState(40000)
  const [cf, setCf] = useState<CounterfactualResponse | null>(null)
  const [decision, setDecision] = useState<string | null>(null)
  const [stress, setStress] = useState<StressRunResponse | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [evalJson, setEvalJson] = useState<unknown>(null)

  useEffect(() => {
    if (state && !state.receipts.some(r => r.id === receiptId)) {
      setReceiptId(state.receipts.find(r => r.id === 'INV-4471')?.id ?? state.receipts[0]?.id ?? '')
    }
  }, [state, receiptId])
  useEffect(() => {
    fetch('/eval.json').then(r => (r.ok && (r.headers.get('content-type') ?? '').includes('json') ? r.json() : null))
      .then(setEvalJson).catch(() => setEvalJson(null))
  }, [])
  useEffect(() => { setCf(null); setDecision(null); setStress(null) }, [rev])

  async function run() {
    setBusy(true); setErr(null)
    try {
      const scenario = { id: 'judge-lab', description: `Receipt ${receiptId} delayed ${delay}d`,
                         receipt_delays: receiptId ? { [receiptId]: delay } : {} }
      // 1) counterfactual (scenario only): constraint report + curves
      setCf(await api.counterfactual({ scenario }))
      // 2) the bill under the same shock is evaluated by the decision endpoint with a what-if amount
      const d = await api.simulate({ proposed_action: { kind: 'payment', amount_cents: amount * 100,
        from_account: state?.accounts[0]?.id ?? 'operating', payee: 'Northwind' }, scenario })
      setDecision(`${d.decision.toUpperCase()}: ${d.explanation}`)
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }

  async function runStress() {
    setBusy(true); setErr(null)
    try {
      const { run_id } = await api.startStress()
      setStress(await api.getStress(run_id))
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }

  return (
    <div className="grid gap-6 lg:grid-cols-2 text-sm">
      <div className="space-y-3">
        <h3 className="font-semibold">Counterfactual: judge a shock</h3>
        <label className="block">
          <span className="text-xs text-slate-500">Delayed receivable</span>
          <select value={receiptId} onChange={e => setReceiptId(e.target.value)} className="mt-1 block w-full rounded border border-slate-300 p-1">
            {state?.receipts.map(r => <option key={r.id} value={r.id}>{r.id} · {r.counterparty} · {usd(r.amount_cents)}</option>)}
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
        <button onClick={run} disabled={busy} className="rounded bg-violet-700 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-50">Run simulation</button>
        {err && <div className="rounded bg-red-50 p-2 text-red-700">{err}</div>}
        {cf && (
          <div className="rounded border border-slate-200 p-3">
            <div className="text-xs font-semibold uppercase text-slate-500">Constraint report</div>
            {cf.constraint_report.length === 0
              ? <p className="mt-1 text-emerald-700">✓ No buffer breach under this scenario.</p>
              : <ul className="mt-1 list-disc pl-5 text-slate-700">{cf.constraint_report.map((r, i) => <li key={i}>{r}</li>)}</ul>}
            <div className="mt-2 text-xs text-slate-500">
              before worst shortfall {usd(cf.before_metrics.worst_shortfall_cents)} → after {usd(cf.after_metrics.worst_shortfall_cents)} · min headroom {usd(cf.after_metrics.min_headroom_cents)}
            </div>
          </div>
        )}
        {decision && <div className="rounded border border-slate-200 p-3 text-slate-700"><b>What-if payment:</b> {decision}</div>}
      </div>

      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <h3 className="font-semibold">Stress tests</h3>
          <button onClick={runStress} disabled={busy} className="rounded border border-slate-300 px-2 py-0.5 text-xs hover:bg-slate-50 disabled:opacity-50">Run stress test</button>
        </div>
        {stress && (
          <div className="rounded border border-slate-200 p-3">
            <div className="text-xs text-slate-500">run <span className="font-mono">{stress.run_id}</span> · {stress.status} · source {stress.source} · {stress.scenarios.length} scenarios</div>
            <ul className="mt-2 space-y-1">
              {stress.findings.map(f => (
                <li key={f.scenario_id} className="flex items-center gap-2">
                  <span className={`rounded px-1.5 text-xs font-semibold ${f.breach ? 'bg-red-100 text-red-800' : 'bg-emerald-100 text-emerald-800'}`}>{f.breach ? '✕ breach' : '✓ ok'}</span>
                  <span className="truncate">{f.label}</span>
                  {f.breach && <span className="ml-auto font-mono text-xs">{usd(f.worst_shortfall_cents)}</span>}
                </li>
              ))}
            </ul>
          </div>
        )}
        <h3 className="pt-2 font-semibold">Policy evaluation (A / B / C)</h3>
        <div className="rounded border border-dashed border-slate-300 p-3 text-xs text-slate-500">
          {evalJson
            ? <pre className="max-h-60 overflow-auto">{JSON.stringify(evalJson, null, 2)}</pre>
            : 'No public/eval.json yet. Run scripts/evaluate.py (Step 9) to populate this chart.'}
        </div>
      </div>
    </div>
  )
}
