import { useEffect, useState } from 'react'
import { Reveal, Section } from './shared'

interface Pol { description: string; mean_breach_rate: number; mean_missed_obligation_rate: number; mean_runtime_ms: number }
interface Search { evaluations: number; unique_breaches: number; fixtures_with_breach_found: number }
interface Ev {
  protocol: { fixtures: number; heldout_paths_per_fixture: number; caveat: string }
  summary: { policies: Record<string, Pol>; search: { adaptive: Search; random: Search } }
}
const ORDER = ['A', 'B', 'B+', 'C']
const pct = (v: number) => `${(v * 100).toFixed(1)}%`

function Bars({ title, rows, max, fmt, label }: { title: string; rows: { k: string; v: number; hi?: boolean; name?: string }[]; max: number; fmt: (v: number) => string; label: string }) {
  return (
    <figure className="min-w-0">
      <figcaption className="text-[15px] font-semibold text-cg-ink">{title}</figcaption>
      <div role="img" aria-label={`${title}: ${rows.map(r => `${r.name ?? r.k} ${fmt(r.v)}`).join(', ')}`} className="mt-4 space-y-3">
        {rows.map(r => (
          <div key={r.k} className="grid grid-cols-[64px_minmax(0,1fr)_56px] items-center gap-3 sm:grid-cols-[84px_minmax(0,1fr)_64px]">
            <span className={`text-[14px] ${r.hi ? 'font-bold text-cg-ink' : 'font-medium text-slate-700'}`}>{r.name ?? r.k}</span>
            <div className="h-6 rounded-[4px] bg-slate-100" title={`${r.name ?? r.k}: ${fmt(r.v)}`}>
              <div className="bar h-full rounded-[4px]" style={{ width: `${Math.max(1.2, (r.v / max) * 100)}%`, background: r.hi ? '#2563eb' : '#64748b' }} />
            </div>
            <span className={`text-right font-mono text-[13px] tabular-nums ${r.hi ? 'font-bold text-cg-ink' : 'text-slate-700'}`}>{fmt(r.v)}</span>
          </div>
        ))}
      </div>
      <p className="mt-3 text-[12.5px] text-slate-600">{label}</p>
    </figure>
  )
}

export default function Evaluation() {
  const [ev, setEv] = useState<Ev | null>(null)
  const [failed, setFailed] = useState(false)
  const [table, setTable] = useState(false)
  useEffect(() => {
    let live = true
    fetch('/eval.json').then(r => (r.ok ? r.json() : Promise.reject(r.status))).then(j => { if (live) setEv(j as Ev) }).catch(() => { if (live) setFailed(true) })
    return () => { live = false }
  }, [])

  const pols = ev?.summary.policies
  return (
    <Section id="evaluation" tone="canvas" title="Does stress-testing before paying actually help?"
      intro={ev ? `We replayed ${ev.protocol.fixtures} synthetic companies against ${ev.protocol.heldout_paths_per_fixture} held-out cash paths each, using four ways of funding the same pay run.` : 'We replayed synthetic companies against held-out cash paths, using four ways of funding the same pay run.'}>
      {!pols && (
        <p className="rounded-lg border border-slate-300 bg-white p-4 text-[15px] text-slate-700" role="status">
          {failed ? 'The evaluation results (eval.json) could not be loaded. Run the evaluation script to regenerate them.' : 'Loading evaluation results...'}
        </p>
      )}
      {ev && pols && (
        <>
          <Reveal className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm sm:p-8">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="text-[14px] text-slate-600">Held-out paths: shocks the policies never saw. Lower is better.</p>
              <button type="button" onClick={() => setTable(t => !t)} aria-pressed={table}
                className="rounded-md border border-slate-300 px-3 py-1.5 text-[13px] font-medium text-slate-700 hover:bg-slate-50">{table ? 'View as chart' : 'View as table'}</button>
            </div>
            {table ? (
              <div className="mt-5 overflow-x-auto">
                <table className="w-full min-w-[480px] text-left text-[14px]">
                  <thead><tr className="border-b border-slate-200 text-slate-600"><th className="py-2 pr-4 font-medium">Policy</th><th className="py-2 pr-4 font-medium">Breach rate</th><th className="py-2 pr-4 font-medium">Missed obligations</th><th className="py-2 font-medium">Mean runtime</th></tr></thead>
                  <tbody>{ORDER.map(k => (
                    <tr key={k} className="border-b border-slate-100 last:border-0"><td className="py-2 pr-4 font-semibold">{k}</td><td className="py-2 pr-4 font-mono">{pct(pols[k].mean_breach_rate)}</td><td className="py-2 pr-4 font-mono">{pct(pols[k].mean_missed_obligation_rate)}</td><td className="py-2 font-mono">{pols[k].mean_runtime_ms.toFixed(1)} ms</td></tr>
                  ))}</tbody>
                </table>
              </div>
            ) : (
              <div className="mt-6 grid gap-10 md:grid-cols-2">
                <Bars title="Held-out breach rate" max={0.6} fmt={pct} label="Share of held-out paths where any account dips below its buffer."
                  rows={ORDER.map(k => ({ k, v: pols[k].mean_breach_rate, hi: k === 'C', name: k === 'C' ? 'C' : k }))} />
                <Bars title="Missed-obligation rate" max={0.2} fmt={pct} label="Mean rate of obligations missed across held-out paths."
                  rows={ORDER.map(k => ({ k, v: pols[k].mean_missed_obligation_rate, hi: k === 'C' }))} />
              </div>
            )}
            <dl className="mt-8 grid gap-x-8 gap-y-3 border-t border-slate-200 pt-6 text-[14px] leading-[1.55] md:grid-cols-2">
              {ORDER.map(k => (
                <div key={k} className="flex gap-3">
                  <dt className={`w-8 shrink-0 font-mono font-bold ${k === 'C' ? 'text-blue-700' : 'text-slate-800'}`}>{k}</dt>
                  <dd className="text-slate-700">{pols[k].description}{k === 'C' && <span className="ml-1.5 font-semibold text-blue-700">This is what CashGuard runs.</span>}</dd>
                </div>
              ))}
            </dl>
          </Reveal>

          <div className="mt-6 grid gap-6 md:grid-cols-2">
            <Reveal className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm sm:p-8">
              <Bars title="Adaptive search vs random search" max={Math.max(ev.summary.search.adaptive.unique_breaches, ev.summary.search.random.unique_breaches) * 1.1} fmt={v => String(Math.round(v))}
                label={`Unique breaches found with the same budget of ${ev.summary.search.adaptive.evaluations} evaluations each.`}
                rows={[{ k: 'adaptive', name: 'Adaptive', v: ev.summary.search.adaptive.unique_breaches, hi: true }, { k: 'random', name: 'Random', v: ev.summary.search.random.unique_breaches }]} />
            </Reveal>
            <Reveal className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm sm:p-8">
              <h3 className="text-[15px] font-semibold text-cg-ink">How to read this</h3>
              <ul className="mt-3 space-y-2.5 text-[14.5px] leading-[1.6] text-slate-700">
                <li>Most of the improvement comes from planning against stress scenarios at all (B to B+): breach rate falls from {pct(pols.B.mean_breach_rate)} to {pct(pols['B+'].mean_breach_rate)}.</li>
                <li>Adding the adaptive search (C) trims it a little further, to {pct(pols.C.mean_breach_rate)}, at a higher compute cost: {pols.C.mean_runtime_ms.toFixed(0)} ms mean per plan offline, against {pols['B+'].mean_runtime_ms.toFixed(0)} ms.</li>
                <li>Adaptive and random search both found breaches in {ev.summary.search.adaptive.fixtures_with_breach_found} of {ev.protocol.fixtures} companies; adaptive found more distinct ones.</li>
              </ul>
            </Reveal>
          </div>
          <p className="mt-6 max-w-[760px] text-[14px] leading-[1.6] text-slate-700">
            <strong className="font-semibold">Synthetic data, documented shock model, not calibrated.</strong> These are frequencies from a prototype&rsquo;s simulations, not a promise about any real company.
          </p>
        </>
      )}
    </Section>
  )
}
