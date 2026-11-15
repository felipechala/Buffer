import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { AuditEvent } from '../types'

export default function EventsTab({ rev }: { rev: number }) {
  const [ev, setEv] = useState<AuditEvent[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const load = () => api.events(200).then(setEv).catch(e => setErr(String(e.message ?? e)))
  const [hideSeed, setHideSeed] = useState(true)
  const lastRev = useRef<number | null>(null)
  useEffect(() => { if (lastRev.current === rev) return; lastRev.current = rev; void load() }, [rev])
  if (err) return <p className="text-sm text-block">{err}</p>
  if (!ev) return <p className="text-sm text-slate-500">Loading events...</p>
  const shown = [...ev].filter(e => !hideSeed || e.actor !== 'seed').sort((a, b) => (b.seq ?? 0) - (a.seq ?? 0))
  return (
    <section className="rounded-lg border border-slate-200">
      <div className="flex flex-wrap items-center gap-3 border-b border-slate-100 px-3 py-2 text-xs">
        <h3 className="sec-title">Audit events</h3>
        <span className="text-slate-500">{shown.length} of {ev.length} shown, newest first</span>
        <label className="ml-auto flex items-center gap-1.5 text-slate-600">
          <input type="checkbox" checked={hideSeed} onChange={e => setHideSeed(e.target.checked)} /> Hide seed events
        </label>
        <button onClick={() => void load()} className="btn">Refresh</button>
      </div>
      <div className="overflow-x-auto px-1 py-1">
        <table className="tbl">
          <thead><tr><th className="r">#</th><th>Time</th><th>Actor</th><th>Action</th><th>Entity</th><th>Correlation</th></tr></thead>
          <tbody>{shown.map((e, i) => (
            <tr key={e.seq ?? i}>
              <td className="r num text-xs text-slate-500">{e.seq}</td>
              <td className="num text-xs text-slate-600">{e.timestamp.slice(11, 19)}</td>
              <td>{e.actor}</td>
              <td className="font-medium text-slate-900">{e.action}</td>
              <td className="font-mono text-xs text-slate-700">{e.entity}:{e.entity_id}</td>
              <td className="mono-id">{e.correlation_id}</td>
            </tr>))}</tbody>
        </table>
      </div>
      {shown.length === 0 && <p className="px-3 py-4 text-sm text-slate-500">No events yet.</p>}
    </section>
  )
}
