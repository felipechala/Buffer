import type { SearchSummary } from '../types'

// Two categorical slots from the dataviz reference palette (blue = adaptive, orange = random).
export const C_ADAPTIVE = '#2a78d6'
export const C_RANDOM = '#eb6834'

interface Metric { label: string; a: number | null | undefined; r: number | null | undefined; hint: string }

export default function SearchCompare({ adaptive, random }: { adaptive?: SearchSummary; random?: SearchSummary }) {
  if (!adaptive || !random) return null
  const metrics: Metric[] = [
    { label: 'Unique breaching shocks found', a: adaptive.unique_breaches, r: random.unique_breaches, hint: 'more is better' },
    { label: 'Evaluations to first breach', a: adaptive.first_breach_found_at, r: random.first_breach_found_at, hint: 'fewer is better' },
    { label: 'Evaluations to first unfixable shock', a: adaptive.first_unfixable_found_at, r: random.first_unfixable_found_at, hint: 'fewer is better' },
  ]
  return (
    <figure className="mt-3 border-t border-slate-200 pt-2">
      <figcaption className="flex flex-wrap items-center gap-3 text-xs">
        <span className="font-semibold text-slate-600">Adaptive search vs random, equal budget ({adaptive.evaluations ?? '?'} evaluations each)</span>
        <span className="flex items-center gap-1 text-slate-600"><i className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: C_ADAPTIVE }} />Adaptive</span>
        <span className="flex items-center gap-1 text-slate-600"><i className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: C_RANDOM }} />Random</span>
      </figcaption>
      <div className="mt-2 space-y-2">
        {metrics.map(m => {
          const max = Math.max(m.a ?? 0, m.r ?? 0, 1)
          const row = (v: number | null | undefined, color: string, name: string) => (
            <div className="flex items-center gap-2" title={`${name}: ${v ?? 'not found'}`}>
              <div className="w-3/4"><div className="h-2.5 rounded-r" style={{ width: `${v == null ? 0 : Math.max(2, (v / max) * 100)}%`, background: color }} /></div>
              <span className="font-mono text-xs tabular-nums text-slate-700">{v == null ? 'not found' : v}</span>
            </div>
          )
          return (
            <div key={m.label}>
              <div className="text-xs text-slate-600">{m.label} <span className="text-slate-400">({m.hint})</span></div>
              <div className="mt-0.5 space-y-0.5">{row(m.a, C_ADAPTIVE, 'Adaptive')}{row(m.r, C_RANDOM, 'Random')}</div>
            </div>
          )
        })}
      </div>
    </figure>
  )
}
