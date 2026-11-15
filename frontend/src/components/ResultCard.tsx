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
  allow: { wrap: 'border-allow-line', banner: 'bg-allow-soft text-allow-ink', pill: 'bg-allow text-white', icon: '✓', label: 'ALLOW', verb: 'Safe to pay' },
  warn: { wrap: 'border-warn-line', banner: 'bg-warn-soft text-warn-ink', pill: 'bg-warn text-white', icon: '!', label: 'WARN', verb: 'Pay with a fix' },
  block: { wrap: 'border-block-line', banner: 'bg-block-soft text-block-ink', pill: 'bg-block text-white', icon: '✕', label: 'BLOCK', verb: 'Do not pay' },
} as const

function describe(a: FixAction): string {
  if (a.kind === 'schedule_bill') return `Pay ${usd(a.amount_cents)} on ${md(a.pay_date)}`
  if (a.kind === 'transfer')
    return `Transfer ${usd(a.amount_cents)} ${a.from_account} → ${a.to_account} on ${md(a.dispatch_date)} (fee ${usd(a.fee_cents)})`
  return `Draw ${usd(a.amount_cents)} on ${a.line_id} into ${a.account} on ${md(a.date)} (fee ${usd(a.fee_cents)})`
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
  const busyFlow = flow != null && flow.step !== 'done' && flow.step !== 'error'
  return (
    <div className={`mt-3 overflow-hidden rounded-lg border bg-white ${s.wrap}`}>
      <div className={`flex flex-wrap items-center gap-x-3 gap-y-1 px-3.5 py-2.5 ${s.banner}`}>
        <span className={`inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-sm font-bold tracking-wide ${s.pill}`}>
          <span aria-hidden>{s.icon}</span>{s.label}
        </span>
        <div className="min-w-0">
          <div className="text-base font-semibold leading-5 text-slate-900">{d.payee}<span className="num ml-2">{usd(d.amount_cents)}</span></div>
          <div className="text-xs text-slate-600">{s.verb} · scheduled {md(d.requested_date)}</div>
        </div>
        <span className="ml-auto rounded-full bg-white/80 px-2.5 py-0.5 text-xs text-slate-600 ring-1 ring-inset ring-black/5" title="Time for the decision call itself (simulation + fix search), excluding the explanation">
          decision in <b className="num text-slate-900">{d.latency_ms} ms</b>
        </span>
      </div>

      <div className="space-y-4 px-3.5 py-3">
        <div>
          <p className="max-w-[68ch] text-base leading-relaxed text-slate-700">{exp?.text ?? d.explanation}</p>
          <div className="mt-1 flex items-center gap-2 text-2xs text-slate-500">
            <span className={`rounded px-1.5 py-px font-medium ${isLlm ? 'bg-violet-50 text-violet-700' : 'bg-slate-100 text-slate-500'}`}>{isLlm ? 'AI explanation' : 'template'}</span>
            {expState === 'loading' && <span className="animate-pulse">writing explanation…</span>}
            {exp && <span className="font-mono" title="Separate call to the explanation endpoint">{exp.source === 'llm' ? `${exp.model ?? 'llm'} · ` : ''}{exp.latency_ms} ms</span>}
          </div>
        </div>

        {groups.length > 0 && (
          <section>
            <h4 className="mb-1 text-sm font-semibold text-slate-900">Where cash breaks</h4>
            <ul className="divide-y divide-slate-100 rounded-md border border-slate-200">
              {groups.slice(0, 5).map((g, i) => { const b = g.first; return (
                <li key={i} className="flex items-center gap-2.5 px-2.5 py-1.5 text-sm">
                  <span className="num w-[84px] shrink-0 text-xs text-slate-600">{range(g.dates)}</span>
                  <span className="chip chip-neutral font-mono">{b.account}</span>
                  <span className="min-w-0 flex-1 truncate text-xs text-slate-500" title={`${b.obligation ? 'obligation ' + b.obligation + ' · ' : ''}${b.scenario.label} (${(b.scenario.plausibility * 100).toFixed(0)}% plausible)`}>
                    {b.obligation ? `${b.obligation} · ` : ''}{b.scenario.label} · {(b.scenario.plausibility * 100).toFixed(0)}% plausible{g.days > 1 ? ` · ${g.days} days` : ''}
                  </span>
                  <span className="num shrink-0 text-right font-medium text-block">short {usd(b.shortfall_cents)}</span>
                </li>
              )})}
            </ul>
          </section>
        )}

        {d.fixes.length > 0 && (
          <section>
            <h4 className="mb-1 text-sm font-semibold text-slate-900">Verified fixes <span className="font-normal text-slate-500">· approve one to apply it</span></h4>
            <div className="space-y-2">
              {d.fixes.map((f, i) => {
                const active = flow?.fixId === f.id
                return (
                  <div key={f.id} className="rounded-lg border border-slate-200 bg-slate-50/60 p-3">
                    <div className="flex items-center gap-2">
                      <span className="flex h-5 w-5 items-center justify-center rounded-full bg-slate-800 text-2xs font-bold text-white">{i + 1}</span>
                      <span className="text-sm font-semibold capitalize text-slate-900">{f.kind}</span>
                      <span className={`chip ${f.verified ? 'chip-ok' : 'chip-neutral'}`}>{f.verified ? '✓ verified' : 'unverified'}</span>
                      <span className="ml-auto flex items-center gap-1.5 text-xs text-slate-500">
                        <span className="chip chip-neutral num">cost {usd(f.cost_cents)}</span>
                        <span className="chip chip-neutral num" title="Breach rate on held-out scenario paths">held-out breach {(f.held_out.breach_rate * 100).toFixed(1)}%</span>
                      </span>
                    </div>
                    <p className="mt-1.5 text-sm text-slate-800">{f.summary}</p>
                    <ul className="mt-1 space-y-0.5 text-xs text-slate-500">
                      {f.actions.map((a, j) => <li key={j} className="flex gap-1.5"><span aria-hidden className="text-slate-300">•</span>{describe(a)}</li>)}
                    </ul>
                    <div className="mt-2.5 flex items-center gap-3">
                      <button disabled={!f.verified || busyFlow} onClick={() => onApprove(f)} className="btn-primary">
                        Approve sandbox action
                      </button>
                      {active && flow && (
                        <span className={`text-xs ${flow.step === 'error' ? 'text-block' : 'text-slate-600'}`}>
                          {flow.step === 'done' ? '✓ ' : ''}{flow.step}{flow.msg ? `: ${flow.msg}` : ''}
                        </span>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          </section>
        )}

        {d.decision === 'block' && d.constraint_report.length > 0 && (
          <details className="rounded-md border border-slate-200 text-xs">
            <summary className="cursor-pointer select-none px-2.5 py-1.5 font-medium text-slate-700 hover:bg-slate-50">Why no fix exists: constraint report ({d.constraint_report.length})</summary>
            <ul className="list-disc space-y-0.5 border-t border-slate-100 py-2 pl-7 pr-3 text-slate-600">
              {d.constraint_report.slice(0, 6).map((r, i) => <li key={i}>{r}</li>)}
            </ul>
          </details>
        )}

        <div className="border-t border-slate-100 pt-2 font-mono text-2xs text-slate-500">
          {d.provenance.input_digest} · {d.provenance.scenarios_checked} scenarios · {d.provenance.engine_version}
        </div>
      </div>
    </div>
  )
}
