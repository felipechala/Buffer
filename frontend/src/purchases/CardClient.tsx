import { useEffect, useState } from 'react'
import type { DecisionLevel, PurchaseRequest } from '../types'
import { usd } from '../api'
import { clock, clockShort, Shield, topBreak, usePurchases } from './shared'

interface AuthRow {
  id: number; req: PurchaseRequest; at: Date
  state: 'pending' | 'approved' | 'declined' | 'error'
  level?: DecisionLevel; latency?: number; reason?: string; note?: string
}

export default function CardClient() {
  const { requests, check, commit, commits, resetKey } = usePurchases()
  const cards = requests.filter(r => r.channel === 'card').sort((a, b) => a.step - b.step)
  const [rows, setRows] = useState<AuthRow[]>([])
  const [n, setN] = useState(1)
  const [busy, setBusy] = useState(false)
  useEffect(() => { setRows([]); setBusy(false) }, [resetKey])

  const patch = (id: number, p: Partial<AuthRow>) => setRows(rs => rs.map(r => (r.id === id ? { ...r, ...p } : r)))

  async function tap(req: PurchaseRequest) {
    const id = n; setN(n + 1); setBusy(true)
    const at = new Date()
    setRows(rs => [{ id, req, at, state: 'pending' }, ...rs])
    try {
      const d = await check(req)
      if (d.decision === 'allow') {
        const r = await commit(req, d, null)
        if (r.ok) patch(id, { state: 'approved', level: 'allow', latency: d.latency_ms })
        else patch(id, { state: 'declined', level: 'allow', latency: d.latency_ms, reason: r.stale ? 'Cash picture changed — retry' : r.message })
      } else {
        const why = topBreak(d)
        patch(id, {
          state: 'declined', level: d.decision, latency: d.latency_ms,
          reason: d.decision === 'warn' ? `Needs finance approval${why ? ' · ' + why : ''}` : why ?? 'No safe way to fund this',
        })
        // shared-twin note: this card tap was safe at the start, a purchase committed elsewhere changed the answer
        const prior = [...commits].reverse().find(c => c.reqId !== req.id)
        if (prior && req.step === 4) patch(id, { note: `Was safe at the start — changed by the ${prior.payee} purchase committed ${clockShort(prior.at)}` })
      }
    } catch (e) {
      patch(id, { state: 'error', reason: e instanceof Error ? e.message : String(e) })
    } finally { setBusy(false) }
  }

  return (
    <section aria-label="Fold card terminal" className="flex h-full flex-col overflow-hidden rounded-xl border border-[#14312b] bg-[#0c1a17] text-[#d9f2ea] shadow-sm">
      <div className="flex items-center gap-2 px-3 py-2">
        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-[#5eead4] text-xs font-extrabold text-[#0c1a17]">f</span>
        <span className="text-sm font-bold">Fold Business Card</span>
        <span className="ml-auto font-mono text-2xs text-[#d9f2ea]/50">•• 4417</span>
      </div>
      <div className="px-3 pb-3">
        <div className="rounded-2xl bg-gradient-to-br from-[#16342d] to-[#0f2420] p-3 ring-1 ring-[#5eead4]/20">
          <div className="text-2xs text-[#d9f2ea]/60">Tap to pay at a merchant</div>
          <div className="mt-2 flex flex-col gap-2">
            {cards.map(c => (
              <button key={c.id} disabled={busy} onClick={() => void tap(c)}
                className="rounded-xl bg-[#5eead4] px-3 py-2 text-left text-sm font-bold text-[#0c1a17] hover:bg-[#8cf2e0] disabled:opacity-40">
                <span className="block text-2xs font-semibold opacity-70">Tap card at</span>
                <span className="flex items-start justify-between gap-2"><span className="min-w-0 break-words leading-5">{c.payee_name}</span><span className="num shrink-0 whitespace-nowrap leading-5">{usd(c.amount_cents)}</span></span>
              </button>
            ))}
            {cards.length === 0 && <span className="text-xs text-[#d9f2ea]/60">loading card requests…</span>}
          </div>
        </div>
      </div>
      <div className="flex items-center border-t border-[#d9f2ea]/10 px-3 py-1.5">
        <span className="text-xs font-semibold">Authorization stream</span>
        <Shield className="ml-auto whitespace-nowrap text-[#d9f2ea]/60" />
      </div>
      <ul className="max-h-[360px] min-h-0 flex-1 divide-y divide-[#d9f2ea]/10 overflow-auto px-3 pb-3 text-xs">
        {rows.length === 0 && <li className="py-3 text-[#d9f2ea]/50">No authorizations yet. Each tap asks CashGuard before the issuer answers.</li>}
        {rows.map(r => (
          <li key={r.id} className="py-2">
            <div className="flex items-center gap-2">
              <span className="num text-[#d9f2ea]/50">{clock(r.at)}</span>
              <span className="min-w-0 flex-1 break-words font-medium">{r.req.payee_name}</span>
              <span className="num whitespace-nowrap">{usd(r.req.amount_cents)}</span>
            </div>
            <div className="mt-1 flex items-center gap-2">
              {r.state === 'pending' && <span className="animate-pulse text-[#d9f2ea]/70">Authorizing…</span>}
              {r.state === 'approved' && <span className="rounded bg-[#5eead4] px-1.5 py-px font-bold text-[#0c1a17]">Approved</span>}
              {(r.state === 'declined' || r.state === 'error') && <span className="rounded bg-[#fda4af] px-1.5 py-px font-bold text-[#4c0519]">{r.level === 'warn' ? 'Declined — needs finance approval' : 'Declined'}</span>}
              {r.latency != null && <span className="num ml-auto text-[#d9f2ea]/50">{r.latency} ms</span>}
            </div>
            {r.reason && <p className="mt-1 leading-4 text-[#d9f2ea]/75">{r.reason}</p>}
            {r.note && <p className="mt-1 rounded bg-[#5eead4]/10 px-2 py-1 leading-4 text-[#9ff3e3]">{r.note}</p>}
          </li>
        ))}
      </ul>
    </section>
  )
}
