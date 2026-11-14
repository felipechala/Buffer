import { useEffect, useState } from 'react'
import { api } from '../api'
import type { AuditEvent } from '../types'

export default function EventsTab({ rev }: { rev: number }) {
  const [ev, setEv] = useState<AuditEvent[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const load = () => api.events(200).then(setEv).catch(e => setErr(String(e.message ?? e)))
  useEffect(() => { void load() }, [rev])
  if (err) return <p className="text-sm text-red-700">{err}</p>
  if (!ev) return <p className="text-sm text-slate-500">Loading events...</p>
  return (
    <div>
      <button onClick={() => void load()} className="mb-2 rounded border border-slate-300 px-2 py-0.5 text-xs hover:bg-slate-50">refresh</button>
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase text-slate-500"><tr><th>#</th><th>Time</th><th>Actor</th><th>Action</th><th>Entity</th><th>Correlation</th></tr></thead>
        <tbody>{ev.map((e, i) => (
          <tr key={e.seq ?? i} className="border-t border-slate-100">
            <td className="py-0.5 font-mono text-xs text-slate-400">{e.seq}</td>
            <td className="font-mono text-xs">{e.timestamp.slice(11, 19)}</td>
            <td>{e.actor}</td>
            <td className="font-medium">{e.action}</td>
            <td className="font-mono text-xs">{e.entity}:{e.entity_id}</td>
            <td className="font-mono text-xs text-slate-500">{e.correlation_id}</td>
          </tr>))}</tbody>
      </table>
      {ev.length === 0 && <p className="text-sm text-slate-500">No events yet.</p>}
    </div>
  )
}
