import { useEffect, useRef, useState } from 'react'
import type { TwinStateResponse } from '../types'
import { usd } from '../api'
import type { CommitLogEntry } from './shared'

function Delta({ cents }: { cents: number }) {
  return <span className="cg-delta pointer-events-none absolute right-2 top-1.5 font-mono text-2xs font-semibold text-block">{cents > 0 ? '+' : ''}{usd(cents)}</span>
}

function Figure({ label, cents, tone = 'text-slate-900', suffix, hint }: { label: string; cents: number; tone?: string; suffix?: string; hint?: string }) {
  const prev = useRef(cents)
  const [delta, setDelta] = useState<{ n: number; k: number } | null>(null)
  useEffect(() => {
    if (prev.current !== cents) { setDelta({ n: cents - prev.current, k: Date.now() }); prev.current = cents }
  }, [cents])
  return (
    <div className="relative min-w-[120px] rounded-lg border border-slate-200 bg-white px-3 py-1.5">
      <div className="text-2xs text-slate-500">{label}{hint && <span className="ml-1 text-slate-400">· {hint}</span>}</div>
      <div className={`num text-lg font-semibold leading-6 ${tone}`}>{usd(cents)}{suffix && <span className="ml-1 text-2xs font-normal text-slate-500">{suffix}</span>}</div>
      {delta && <Delta key={delta.k} cents={delta.n} />}
    </div>
  )
}

export default function CashStrip({ state, commits }: { state: TwinStateResponse | null; commits: CommitLogEntry[] }) {
  const total = commits.reduce((s, c) => s + c.amount_cents, 0)
  return (
    <section aria-label="Cash headroom" className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-slate-200 bg-slate-50/70 px-3 py-2">
      <div className="mr-1">
        <div className="text-sm font-semibold text-slate-900">Shared cash twin</div>
        <div className="text-2xs text-slate-500">lowest projected headroom over 14 days, same twin for all three apps</div>
      </div>
      {state ? state.accounts.map(a => (
        <Figure key={a.id} label={`${a.name} · lowest (14d)`}
          cents={a.min_headroom_cents ?? a.observed_balance_cents - a.buffer_cents}
          hint={a.min_headroom_date ? new Date(a.min_headroom_date + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : undefined} />
      )) : <span className="text-xs text-slate-500">loading twin state…</span>}
      <div className="ml-auto">
        <Figure label={`Committed purchases · ${commits.length}`} cents={total} tone={commits.length ? 'text-cg-accent' : 'text-slate-400'} />
      </div>
    </section>
  )
}
