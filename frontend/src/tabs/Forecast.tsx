import { useEffect, useState } from 'react'
import { Area, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, usd } from '../api'
import type { AccountForecast, ForecastResponse } from '../types'

// Palette roles from the dataviz reference palette (light mode): categorical slots 1-2 + neutral inks.
const C = { baseline: '#2a78d6', stress: '#eb6834', buffer: '#52514e', grid: '#e7e6e2', muted: '#52514e' }

const short = (cents: number) => {
  const v = cents / 100
  return Math.abs(v) >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : Math.abs(v) >= 1e3 ? `${Math.round(v / 1e3)}k` : `${v}`
}

interface Row { date: string; baseline: number; p10: number; p90: number; band: [number, number]; worst: number; buffer: number }

function rows(a: AccountForecast): Row[] {
  return a.dates.map((d, i) => ({
    date: d.slice(5), baseline: a.baseline[i], p10: a.p10[i], p90: a.p90[i],
    band: [a.p10[i], a.p90[i]], worst: a.worst_stress[i], buffer: a.buffer_cents,
  }))
}

function Tip({ active, payload, label }: { active?: boolean; payload?: { payload: Row }[]; label?: string }) {
  if (!active || !payload?.length) return null
  const r = payload[0].payload
  return (
    <div className="rounded border border-slate-200 bg-white p-2 text-xs shadow">
      <div className="mb-1 font-semibold">{label}</div>
      <div>Baseline <b className="font-mono">{usd(r.baseline)}</b></div>
      <div>p10 – p90 <b className="font-mono">{usd(r.p10)} – {usd(r.p90)}</b></div>
      <div>Worst stress <b className="font-mono">{usd(r.worst)}</b></div>
      <div>Buffer <b className="font-mono">{usd(r.buffer)}</b></div>
    </div>
  )
}

function Panel({ a }: { a: AccountForecast }) {
  const data = rows(a)
  const breach = a.worst_stress.some(v => v < a.buffer_cents)
  return (
    <figure className="rounded-lg border border-slate-200 p-3">
      <figcaption className="mb-1 flex items-center text-sm font-semibold text-slate-800">
        {a.account_id}
        {breach && <span className="ml-2 rounded bg-red-100 px-1.5 text-[10px] font-semibold text-red-800">✕ stress breach</span>}
        <span className="ml-auto text-xs font-normal text-slate-500">buffer {usd(a.buffer_cents)}</span>
      </figcaption>
      <div style={{ height: 190 }}>
        <ResponsiveContainer>
          <ComposedChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
            <XAxis dataKey="date" tick={{ fontSize: 10, fill: C.muted }} stroke={C.grid} interval={2} />
            <YAxis tickFormatter={short} tick={{ fontSize: 10, fill: C.muted }} stroke={C.grid} width={44} />
            <Tooltip content={<Tip />} />
            <Area dataKey="band" stroke="none" fill={C.baseline} fillOpacity={0.14} isAnimationActive={false} />
            <ReferenceLine y={a.buffer_cents} stroke={C.buffer} strokeDasharray="2 3" strokeWidth={1} />
            <Line dataKey="worst" stroke={C.stress} strokeWidth={2} strokeDasharray="6 4" dot={false} isAnimationActive={false} />
            <Line dataKey="baseline" stroke={C.baseline} strokeWidth={2} dot={false} activeDot={{ r: 4, stroke: '#fff', strokeWidth: 2 }} isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </figure>
  )
}

export default function ForecastTab({ rev }: { rev: number }) {
  const [f, setF] = useState<ForecastResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    setErr(null)
    api.forecast({ scenario_count: 200, seed: 42 }).then(r => live && setF(r)).catch(e => live && setErr(String(e.message ?? e)))
    return () => { live = false }
  }, [rev])
  if (err) return <p className="text-sm text-red-700">{err}</p>
  if (!f) return <p className="text-sm text-slate-500">Computing forecast...</p>
  return (
    <div>
      <ul className="mb-3 flex flex-wrap gap-4 text-xs text-slate-700">
        <li><span className="mr-1 inline-block h-0.5 w-5 align-middle" style={{ background: C.baseline }} />Baseline balance</li>
        <li><span className="mr-1 inline-block h-3 w-5 align-middle" style={{ background: C.baseline, opacity: 0.14 }} />p10–p90 (200 paths)</li>
        <li><span className="mr-1 inline-block w-5 align-middle" style={{ borderTop: `2px dashed ${C.stress}` }} />Worst stress scenario</li>
        <li><span className="mr-1 inline-block w-5 align-middle" style={{ borderTop: `1px dotted ${C.buffer}` }} />Minimum buffer</li>
        <li className="ml-auto text-slate-500">stress set {f.stress_set_id} · snapshot {f.snapshot_id.slice(0, 12)}</li>
      </ul>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {f.accounts.map(a => <Panel key={a.account_id} a={a} />)}
      </div>
      <p className="mt-2 text-xs text-slate-500">{String(f.provenance.note ?? '')}</p>
    </div>
  )
}
