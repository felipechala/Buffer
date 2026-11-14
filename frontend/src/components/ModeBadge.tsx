import type { ConnectionsResponse } from '../types'

export default function ModeBadge({ conn }: { conn: ConnectionsResponse | null }) {
  if (!conn) return <span className="rounded-full bg-slate-200 px-2 py-0.5 text-xs text-slate-600">connecting...</span>
  const nessie = conn.mode === 'nessie'
  return (
    <span className="flex items-center gap-2 text-xs">
      <span className={`rounded-full px-2 py-0.5 font-semibold ${nessie ? 'bg-indigo-100 text-indigo-800' : 'bg-slate-200 text-slate-700'}`}>
        {nessie ? 'Nessie sandbox' : 'Fixture mode'}
      </span>
      <span className="text-slate-500">writes {conn.writes_enabled ? 'enabled' : 'simulated'}</span>
      {conn.snapshot_id && <span className="font-mono text-slate-400">{conn.snapshot_id.slice(0, 14)}</span>}
    </span>
  )
}
