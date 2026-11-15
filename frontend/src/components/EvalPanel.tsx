import { useEffect, useRef, useState } from 'react'

const BLUE = '#2a78d6'
const GRAY = '#9a99a0'

interface PolicySummary {
  description?: string
  mean_breach_rate?: number
  mean_missed_obligation_rate?: number
  total_modeled_cost_cents?: number
  worst_shortfall_cents?: number
}
interface EvalData {
  protocol?: { fixtures?: number; heldout_paths_per_fixture?: number; caveat?: string; policies?: Record<string, string> }
  summary?: { policies?: Record<string, PolicySummary>; search?: { adaptive?: Record<string, number>; random?: Record<string, number> } }
}

const pct = (v: number) => `${(v * 100).toFixed(v < 0.1 ? 1 : 0)}%`
const dollars = (c: number) => '$' + Math.round(c / 100).toLocaleString('en-US')

function Bars({ title, note, rows, fmt, highlight }: {
  title: string; note: string; rows: { key: string; v: number }[]; fmt: (v: number) => string; highlight: string
}) {
  const max = Math.max(...rows.map(r => r.v), 1e-9)
  return (
    <figure>
      <figcaption className="text-xs font-semibold text-slate-800">{title} <span className="font-normal text-slate-500">({note})</span></figcaption>
      <div className="mt-1.5 space-y-1">
        {rows.map(r => (
          <div key={r.key} className="flex items-center gap-2" title={`Policy ${r.key}: ${fmt(r.v)}`}>
            <span className={`w-7 shrink-0 text-right font-mono text-xs font-semibold ${r.key === highlight ? "text-cg-accent" : "text-slate-600"}`}>{r.key}</span>
            <div className="h-4 flex-1 pr-2">
              <div className="h-4 rounded-r" style={{ width: `${Math.max(r.v / max * 100, r.v > 0 ? 1.5 : 0)}%`, background: r.key === highlight ? BLUE : GRAY }} />
            </div>
            <span className={`w-16 shrink-0 text-right font-mono text-xs tabular-nums ${r.key === highlight ? "font-semibold text-slate-900" : "text-slate-600"}`}>{fmt(r.v)}</span>
          </div>
        ))}
      </div>
    </figure>
  )
}

export default function EvalPanel() {
  const [data, setData] = useState<EvalData | null>(null)
  const [loaded, setLoaded] = useState(false)
  const asked = useRef(false)
  useEffect(() => {
    if (asked.current) return
    asked.current = true
    fetch(`${import.meta.env.BASE_URL}eval.json`)
      .then(r => (r.ok && (r.headers.get('content-type') ?? '').includes('json') ? r.json() : null))
      .then(setData).catch(() => setData(null)).finally(() => setLoaded(true))
  }, [])

  if (!loaded) return <p className="text-xs text-slate-500">Loading evaluation...</p>
  const pol = data?.summary?.policies
  if (!pol || Object.keys(pol).length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-slate-300 p-3 text-xs text-slate-600">
        <p className="font-medium text-slate-700">No evaluation results yet.</p>
        <p className="mt-1">Generate them once (about a minute) from the repo root, then reload this page:</p>
        <code className="mt-1 block rounded bg-slate-100 px-2 py-1 font-mono">python scripts/evaluate.py --quick</code>
      </div>
    )
  }
  const keys = Object.keys(pol)
  const best = keys[keys.length - 1]
  const num = (k: string, f: keyof PolicySummary) => Number(pol[k][f] ?? 0)
  const search = data?.summary?.search
  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-600">
        {data?.protocol?.fixtures ?? '?'} fixtures x {data?.protocol?.heldout_paths_per_fixture ?? '?'} held-out paths. Plans are made
        without seeing these paths. Lower is better for every measure. Policy {best} (Buffer) is highlighted.
      </p>
      <Bars title="Held-out breach rate" note="share of paths that dip below a buffer" highlight={best}
        rows={keys.map(k => ({ key: k, v: num(k, 'mean_breach_rate') }))} fmt={pct} />
      <Bars title="Missed-obligation rate" note="share of paths with an unpaid bill" highlight={best}
        rows={keys.map(k => ({ key: k, v: num(k, 'mean_missed_obligation_rate') }))} fmt={pct} />
      <Bars title="Modeled funding cost" note="fees, summed over fixtures" highlight={best}
        rows={keys.map(k => ({ key: k, v: num(k, 'total_modeled_cost_cents') }))} fmt={dollars} />
      <details className="text-xs text-slate-600">
        <summary className="cursor-pointer">Policy definitions and table</summary>
        <table className="mt-1 w-full text-left">
          <thead className="text-2xs text-slate-500">
            <tr><th>Policy</th><th>Description</th><th className="text-right">Breach</th><th className="text-right">Missed</th><th className="text-right">Cost</th><th className="text-right">Worst short</th></tr>
          </thead>
          <tbody>{keys.map(k => (
            <tr key={k} className="border-t border-slate-100 align-top">
              <td className="py-0.5 font-mono font-semibold">{k}</td>
              <td>{pol[k].description ?? data?.protocol?.policies?.[k] ?? ''}</td>
              <td className="text-right font-mono">{pct(num(k, 'mean_breach_rate'))}</td>
              <td className="text-right font-mono">{pct(num(k, 'mean_missed_obligation_rate'))}</td>
              <td className="text-right font-mono">{dollars(num(k, 'total_modeled_cost_cents'))}</td>
              <td className="text-right font-mono">{dollars(num(k, 'worst_shortfall_cents'))}</td>
            </tr>))}</tbody>
        </table>
      </details>
      {search?.adaptive && search?.random && (
        <p className="border-t border-slate-100 pt-3 text-xs text-slate-600">
          Across all fixtures at equal search budget, adaptive search found <b>{search.adaptive.unique_breaches ?? '?'}</b> unique breaching
          shocks vs <b>{search.random.unique_breaches ?? '?'}</b> for random, and an unfixable shock in{' '}
          <b>{search.adaptive.fixtures_with_unfixable_found ?? '?'}</b> vs <b>{search.random.fixtures_with_unfixable_found ?? '?'}</b> fixtures.
        </p>
      )}
      {data?.protocol?.caveat && <p className="text-2xs text-slate-500">{data.protocol.caveat}</p>}
    </div>
  )
}
