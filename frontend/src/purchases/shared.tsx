import { createContext, useContext, useEffect, useRef, useState } from 'react'
import type { CommitResponse, Decision, DecisionLevel, ExplanationResponse, Fix, PurchaseRequest } from '../types'
import { api, usd } from '../api'
import { groupBreaks, md } from '../components/ResultCard'

export interface CommitLogEntry {
  purchaseId: string; reqId: string; step: number; payee: string; amount_cents: number
  channel: string; scheduled: string; at: Date
}
export interface StepState { decision: DecisionLevel; committed: boolean }
export type CommitResult =
  | { ok: true; res: CommitResponse }
  | { ok: false; stale: true; decision: Decision }
  | { ok: false; stale?: false; message: string }

export interface PurchaseApi {
  requests: PurchaseRequest[]
  loadError: string | null
  check: (req: PurchaseRequest) => Promise<Decision>
  commit: (req: PurchaseRequest, d: Decision, fix: Fix | null) => Promise<CommitResult>
  commits: CommitLogEntry[]
  steps: Record<string, StepState>
  acctName: (id: string) => string
  resetKey: number
}
export const PurchaseCtx = createContext<PurchaseApi | null>(null)
export function usePurchases(): PurchaseApi {
  const v = useContext(PurchaseCtx)
  if (!v) throw new Error('PurchaseCtx missing')
  return v
}

export const money = (c: number) => (c % 100 === 0 ? usd(c) : '$' + (c / 100).toFixed(2))
export const clock = (d: Date) => d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
export const clockShort = (d: Date) => d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' })

export function fixLabel(f: Fix, acct: (id: string) => string): string {
  const cost = money(f.cost_cents)
  const tr = f.actions.filter((a): a is Extract<Fix['actions'][number], { kind: 'transfer' }> => a.kind === 'transfer')
  const moved = tr.reduce((s, a) => s + a.amount_cents, 0)
  const from = tr[0] ? acct(tr[0].from_account).toLowerCase() : ''
  if (f.kind === 'reschedule') return `Wait until ${md(f.pay_date)} · ${cost}`
  const move = `move ${usd(moved)} from ${from}`
  if (f.kind === 'funding') return `Pay today, ${move} · ${cost}`
  return `Wait until ${md(f.pay_date)}, ${move} · ${cost}`
}

export function topBreak(d: Decision): string | null {
  const g = groupBreaks(d.breaks)[0]
  if (!g) return null
  const b = g.first
  const what = b.obligation ? b.obligation : `${b.account} buffer`
  return `${md(b.date)}: ${what} would be ${usd(b.shortfall_cents)} short (${b.scenario.label})`
}

/** Fetches the explanation_url once per decision (same pattern as ResultCard). */
export function useExplanation(d: Decision | null) {
  const [exp, setExp] = useState<ExplanationResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const asked = useRef<string | null>(null)
  const url = d?.explanation_url ?? null
  useEffect(() => {
    setExp(null)
    if (!url) { setLoading(false); return }
    if (asked.current === url) return
    asked.current = url
    setLoading(true)
    api.explanation(url).then(r => { setExp(r); setLoading(false) }).catch(() => setLoading(false))
  }, [url])
  return { text: exp?.text ?? d?.explanation ?? '', llm: exp?.source === 'llm', loading }
}

export function ExplText({ text, loading, tone = 'text-slate-700' }: { text: string; loading: boolean; tone?: string }) {
  const [open, setOpen] = useState(false)
  const long = text.length > 170
  return (
    <div>
      <p className={`text-sm leading-5 ${tone} ${long && !open ? 'line-clamp-3' : ''}`}>{text}{loading && <span className="ml-1 animate-pulse text-2xs text-slate-400">writing…</span>}</p>
      {long && <button onClick={() => setOpen(!open)} className="mt-0.5 text-2xs font-medium text-slate-500 underline-offset-2 hover:underline">{open ? 'Show less' : 'Show more'}</button>}
    </div>
  )
}

export const TONE = {
  allow: { pill: 'bg-allow text-white', soft: 'bg-allow-soft text-allow-ink', line: 'border-allow-line', icon: '✓', label: 'ALLOW' },
  warn: { pill: 'bg-warn text-white', soft: 'bg-warn-soft text-warn-ink', line: 'border-warn-line', icon: '!', label: 'WARN' },
  block: { pill: 'bg-block text-white', soft: 'bg-block-soft text-block-ink', line: 'border-block-line', icon: '✕', label: 'BLOCK' },
} as const

export function DecisionPill({ level, small }: { level: DecisionLevel; small?: boolean }) {
  const t = TONE[level]
  return (
    <span className={`inline-flex items-center gap-1 rounded-md font-bold tracking-wide ${t.pill} ${small ? 'px-1.5 py-px text-2xs' : 'px-2 py-0.5 text-xs'}`}>
      <span aria-hidden>{t.icon}</span>{t.label}
    </span>
  )
}

export function Shield({ className = '' }: { className?: string }) {
  return (
    <span className={`inline-flex items-center gap-1 text-2xs font-medium ${className}`}>
      <svg width="12" height="12" viewBox="0 0 36 36" aria-hidden>
        <path d="M18 3l13 5v10c0 8-5.5 13.8-13 17.5C10.500 31.800 5 26 5 18V8z" fill="#2563eb" />
        <path d="M12.500 18.200l4 4 7.500-8" fill="none" stroke="#fff" strokeWidth="3.200" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      checked by CashGuard
    </span>
  )
}

export function Dots({ className = 'bg-slate-400' }: { className?: string }) {
  return (
    <span className="inline-flex items-center gap-1" role="status" aria-label="CashGuard is checking">
      {[0, 1, 2].map(i => (
        <span key={i} className={`h-1.5 w-1.5 animate-bounce rounded-full ${className}`} style={{ animationDelay: `${i * 120}ms` }} />
      ))}
    </span>
  )
}
