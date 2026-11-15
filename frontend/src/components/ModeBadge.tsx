import type { ConnectionsResponse } from '../types'

// Rendered on the dark CashGuard header.
export default function ModeBadge({ conn }: { conn: ConnectionsResponse | null }) {
  if (!conn) return <span className="rounded-full bg-white/10 px-2.5 py-0.5 text-xs text-slate-300">connecting...</span>
  const nessie = conn.mode === 'nessie'
  return (
    <span className="flex items-center gap-2 text-xs">
      <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 font-medium ${nessie ? 'bg-indigo-400/20 text-indigo-200' : 'bg-white/10 text-slate-200'}`}>
        <span className={`h-1.5 w-1.5 rounded-full ${nessie ? 'bg-indigo-300' : 'bg-emerald-400'}`} />
        {nessie ? 'Nessie sandbox' : 'Fixture mode'}
      </span>
      <span className="text-slate-400">writes {conn.writes_enabled ? 'enabled' : 'simulated'}</span>
      {conn.snapshot_id && <span className="font-mono text-slate-400" title="Twin snapshot id">{conn.snapshot_id.slice(0, 14)}</span>}
    </span>
  )
}
