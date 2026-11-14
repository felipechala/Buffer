import { useEffect, useRef, useState } from 'react'
import type { Break, Decision, ExplanationResponse, Fix, FixAction } from '../types'
import { api, usd } from '../api'

interface BreakGroup { first: Break; dates: string[]; days: number }
export function groupBreaks(breaks: Break[]): BreakGroup[] {
  const sorted = [...breaks].sort((a, b) => a.day - b.day)
  const out: BreakGroup[] = []
  for (const b of sorted) {
    const g = out.find(x => x.first.account === b.account && x.first.shortfall_cents === b.shortfall_cents
      && x.first.scenario.label === b.scenario.label && x.first.obligation === b.obligation)
    if (g) { g.dates.push(b.date); g.days++ } else out.push({ first: b, dates: [b.date], days: 1 })
  }
  return out
}
export const md = (iso: string) => new Date(iso + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
export const range = (ds: string[]) => ds.length === 1 ? md(ds[0]) : `${md(ds[0])} – ${md(ds[ds.length - 1])}`


const STYLE = {
  allow: { box: 'border-emerald-300 bg-emerald-50', pill: 'bg-emerald-600 text-white', icon: '✓', label: 'ALLOW' },
  warn: { box: 'border-amber-300 bg-amber-50', pill: 'bg-amber-500 text-white', icon: '!', label: 'WARN' },
  block: { box: 'border-red-300 bg-red-50', pill: 'bg-red-600 text-white', icon: '✕', label: 'BLOCK' },
} as const

function describe(a: FixAction): string {
  if (a.kind === 'schedule_bill') return `Pay ${usd(a.amount_cents)} on ${a.pay_date}`
  if (a.kind === 'transfer')
    return `Transfer ${usd(a.amount_cents)} ${a.from_account} → ${a.to_account} on ${a.dispatch_date} (fee ${usd(a.fee_cents)})`
  return `Draw ${usd(a.amount_cents)} on ${a.line_id} into ${a.account} on ${a.date} (fee ${usd(a.fee_cents)})`
}

export type FixFlowState = { fixId: string; step: 'approving' | 'executing' | 'syncing' | 'rechecking' | 'done' | 'error'; msg?: string }

interface Props {
  d: Decision
  flow: FixFlowState | null
  onApprove: (fix: Fix) => void
}

export default function ResultCard({ d, flow, onApprove }: Props) {
  const s = STYLE[d.decision]
  const [exp, setExp] = useState<ExplanationResponse | null>(null)
  const [expState, setExpState] = useState<'idle' | 'loading' | 'failed'>('idle')
  const asked = useRef<string | null>(null)
  useEffect(() => {
    setExp(null)
    const url = d.explanation_url
    if (!url) { setExpState('idle'); return }
    if (asked.current === url) return        // StrictMode double-invoke guard
    asked.current = url
    setExpState('loading')
    api.explanation(url).then(r => { setExp(r); setExpState('idle') }).catch(() => setExpState('failed'))
  }, [d.explanation_url])
  const isLlm = exp ? exp.source === 'llm' : false
  const groups = groupBreaks(d.breaks)
  return (
    <div className={`mt-2 rounded-lg border p-3 text-sm ${s.box}`}>
      <div className="flex items-center gap-2">
        <span className={`inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs font-bold ${s.pill}`}>
          <span aria-hidden>{s.icon}</span>{s.label}
        </span>
        <span className="font-medium text-slate-800">{d.payee} · {usd(d.amount_cents)} on {d.requested_date}</span>
        <span className="ml-auto rounded bg-white/70 px-2 py-0.5 text-xs text-slate-600" title="Time for the decision call itself (simulation + fix search), excluding the explanation">decision in <b className="font-mono text-sm text-slate-900">{d.latency_ms} ms</b></span>
      </div>
      <p className="mt-2 text-slate-700">
        {exp?.text ?? d.explanation}{' '}
        <span className={`rounded px-1.5 py-0.5 align-middle text-[10px] font-semibold uppercase ${isLlm ? 'bg-violet-100 text-violet-700' : 'bg-slate-100 text-slate-500'}`}>
          {isLlm ? 'AI explanation' : 'template'}
        </span>
        {expState === 'loading' && <span className="ml-2 animate-pulse text-xs text-slate-400">writing explanation…</span>}
        {exp && <span className="ml-2 font-mono text-[11px] text-slate-400" title="Separate call to the explanation endpoint">{exp.source === 'llm' ? `${exp.model ?? 'llm'} · ` : ''}{exp.latency_ms} ms</span>}
      </p>

      {groups.length > 0 && (
        <div className="mt-2">
          <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">Breaks</div>
          <ul className="mt-1 space-y-0.5">
            {groups.slice(0, 5).map((g, i) => { const b = g.first; return (
              <li key={i} className="text-slate-700">
                <span className="font-mono text-xs">{range(g.dates)}</span> {b.account} short {usd(b.shortfall_cents)}
                {g.days > 1 ? <span className="text-slate-500"> ({g.days} days)</span> : null}
                {b.obligation ? <> (obligation {b.obligation})</> : null}
                <span className="text-slate-500"> — {b.scenario.label} ({(b.scenario.plausibility * 100).toFixed(0)}% plausible)</span>
              </li>
            )})}
          </ul>
        </div>
      )}

      {d.fixes.length > 0 && (
        <div className="mt-3">
          <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">Verified fixes</div>
          <div className="mt-1 space-y-2">
            {d.fixes.map((f, i) => {
              const active = flow?.fixId === f.id
              return (
                <div key={f.id} className="rounded border border-white/70 bg-white p-2 shadow-sm">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold text-slate-800">{i + 1}. {f.kind}</span>
                    <span className={`rounded px-1.5 text-xs ${f.verified ? 'bg-emerald-100 text-emerald-800' : 'bg-slate-200 text-slate-600'}`}>
                      {f.verified ? '✓ verified' : 'unverified'}
                    </span>
                    <span className="ml-auto text-xs text-slate-500">cost {usd(f.cost_cents)} · held-out breach {(f.held_out.breach_rate * 100).toFixed(1)}%</span>
                  </div>
                  <p className="text-slate-600">{f.summary}</p>
                  <ul className="mt-1 list-disc pl-5 text-xs text-slate-600">
                    {f.actions.map((a, j) => <li key={j}>{describe(a)}</li>)}
                  </ul>
                  <div className="mt-2 flex items-center gap-2">
                    <button
                      disabled={!f.verified || (flow != null && flow.step !== 'done' && flow.step !== 'error')}
                      onClick={() => onApprove(f)}
                      className="rounded bg-violet-700 px-3 py-1 text-xs font-semibold text-white hover:bg-violet-800 disabled:opacity-40">
                      Approve sandbox action
                    </button>
                    {active && flow && (
                      <span className={`text-xs ${flow.step === 'error' ? 'text-red-700' : 'text-slate-600'}`}>
                        {flow.step === 'done' ? '✓ ' : ''}{flow.step}{flow.msg ? `: ${flow.msg}` : ''}
                      </span>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {d.decision === 'block' && d.constraint_report.length > 0 && (
        <div className="mt-3">
          <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">Constraint report</div>
          <ul className="mt-1 list-disc pl-5 text-xs text-slate-700">
            {d.constraint_report.slice(0, 6).map((r, i) => <li key={i}>{r}</li>)}
          </ul>
        </div>
      )}
      <div className="mt-2 font-mono text-[11px] text-slate-400">
        {d.provenance.input_digest} · {d.provenance.scenarios_checked} scenarios · {d.provenance.engine_version}
      </div>
    </div>
  )
}
