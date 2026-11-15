import { useEffect, useRef, useState } from 'react'
import type { TwinStateResponse } from '../types'
import { usd } from '../api'
import type { CommitLogEntry } from './shared'

function Delta({ cents }: { cents: number }) {
  return <span className="cg-delta pointer-events-none absolute right-2 top-1.5 font-mono text-2xs font-semibold text-block">{cents > 0 ? '+' : ''}{usd(cents)}</span>
}

function Figure({ label, cents, tone = 'text-slate-900', suffix, hint, accent }: { label: string; cents: number; tone?: string; suffix?: string; hint?: string; accent?: boolean }) {
  const prev = useRef(cents)
  const [delta, setDelta] = useState<{ n: number; k: number } | null>(null)
  useEffect(() => {
    if (prev.current !== cents) { setDelta({ n: cents - prev.current, k: Date.now() }); prev.current = cents }
  }, [cents])
  return (
    <div className={`relative min-w-0 rounded-lg border px-3 py-2 ${accent ? 'border-brand/40 bg-brand-soft/60' : 'border-slate-200 bg-white'}`}>
      <div className="flex items-baseline justify-between gap-2 text-2xs text-slate-500"><span className="sm:truncate" title={label}>{label}</span>{hint && <span className="shrink-0 text-slate-400">{hint}</span>}</div>
      <div className={`num text-lg font-semibold leading-6 ${tone}`}>{usd(cents)}{suffix && <span className="ml-1 text-2xs font-normal text-slate-500">{suffix}</span>}</div>
      {delta && <Delta key={delta.k} cents={delta.n} />}
    </div>
  )
}

export default function CashStrip({ state, commits }: { state: TwinStateResponse | null; commits: CommitLogEntry[] }) {
  const total = commits.reduce((s, c) => s + c.amount_cents, 0)
  return (
    <section aria-label="Cash headroom" className="mb-3 rounded-xl border border-slate-200 bg-slate-50/70 p-3">
      <div className="mb-2">
        <div className="text-sm font-semibold text-slate-900">Shared cash twin</div>
        <div className="text-2xs text-slate-500">lowest projected headroom over 14 days, same twin for all three apps</div>
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4 2xl:grid-cols-7">
      {state ? state.accounts.map(a => (
        <Figure key={a.id} label={a.name}
          cents={a.min_headroom_cents ?? a.observed_balance_cents - a.buffer_cents}
          hint={a.min_headroom_date ? new Date(a.min_headroom_date + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : undefined} />
      )) : <span className="col-span-full text-xs text-slate-500">loading twin state…</span>}
        <Figure accent label={`Committed · ${commits.length}`} cents={total} tone={commits.length ? 'text-brand' : 'text-slate-400'} />
      </div>
    </section>
  )
}
