import { useState } from 'react'
import { api, prettyId, usd } from '../api'
import type { Decision, Fix, TwinStateResponse } from '../types'
import ResultCard, { md, type FixFlowState } from './ResultCard'

// Demo bills the "third party" app always shows; others come from /v1/twin/state obligations.
const DEMO_BILLS = [
  { id: 'BILL-LEASE-11', payee: 'Lessor', note: 'Office lease' },
  { id: 'BILL-NW-4471', payee: 'Northwind', note: 'Invoice 4471 - supplier' },
  { id: 'BILL-EQUIP-9', payee: 'Equipment vendor', note: 'Big equipment (draft)' },
]
const DEMO_AMOUNTS: Record<string, number> = {
  'BILL-LEASE-11': 500000, 'BILL-NW-4471': 4000000, 'BILL-EQUIP-9': 45000000,
}
const APPROVER = 'u_maya' // finance lead in the demo company

interface Row { id: string; payee: string; note: string; amount_cents: number; due?: string; featured: boolean }

export default function MockClient({ state, refresh }: { state: TwinStateResponse | null; refresh: () => Promise<void> }) {
  const [results, setResults] = useState<Record<string, Decision>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [flow, setFlow] = useState<FixFlowState | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const obl = new Map((state?.obligations ?? []).map(o => [o.id, o]))
  const rows: Row[] = DEMO_BILLS.map(b => {
    const o = obl.get(b.id)
    return { id: b.id, payee: o ? (o.payee_name || prettyId(o.counterparty)) : b.payee, note: b.note, amount_cents: o?.amount_cents ?? DEMO_AMOUNTS[b.id],
             due: o?.due_date, featured: true }
  })
  for (const o of state?.obligations ?? []) {
    if (!DEMO_BILLS.some(b => b.id === o.id))
      rows.push({ id: o.id, payee: o.payee_name || prettyId(o.counterparty), note: o.category, amount_cents: o.amount_cents, due: o.due_date, featured: false })
  }

  async function pay(billId: string) {
    setBusy(billId); setErr(null)
    try {
      const d = await api.simulate({ snapshot_id: state?.snapshot_id, proposed_action: { kind: 'payment', bill_id: billId } })
      setResults(r => ({ ...r, [billId]: d }))
    } catch (e) { setErr(String(e instanceof Error ? e.message : e)) } finally { setBusy(null) }
  }

  async function approveAndExecute(d: Decision, fix: Fix) {
    const key = `exec-${fix.id}-${Date.now()}`
    try {
      setFlow({ fixId: fix.id, step: 'approving' })
      await api.approve(fix.id, APPROVER, 'Approved from Ledgerly AP')
      setFlow({ fixId: fix.id, step: 'executing' })
      const ex = await api.execute(fix.id, key)
      const rec = ex.reconciliation
      const summary = `${ex.mode}/${ex.state}` + (rec ? `, unexplained ${rec.unexplained_cents}c` : '')
        + (ex.state === 'uncertain' && ex.next_step ? ` - ${ex.next_step}` : '')
      setFlow({ fixId: fix.id, step: 'syncing', msg: summary })
      if (ex.mode === 'simulated') await api.syncFixture()   // sandbox mode already re-synced the provider
      await refresh()
      if (ex.state === 'uncertain') { setFlow({ fixId: fix.id, step: 'error', msg: summary }); return }
      setFlow({ fixId: fix.id, step: 'rechecking' })
      const nd = await api.simulate({ proposed_action: { kind: 'payment', bill_id: d.bill_id } })
      setResults(r => ({ ...r, [d.bill_id]: nd }))
      setFlow({ fixId: fix.id, step: 'done', msg: `re-check: ${nd.decision}` })
    } catch (e) {
      setFlow({ fixId: fix.id, step: 'error', msg: String(e instanceof Error ? e.message : e) })
    }
  }

  const featured = rows.filter(r => r.featured)
  const others = rows.filter(r => !r.featured)
  const renderRow = (r: Row) => {
    const d = results[r.id]
    return (
      <li key={r.id} id={'bill-' + r.id} className={`scroll-mt-4 px-4 ${r.featured ? 'py-3.5' : 'py-2.5'}`}>
        <div className="flex items-center gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline gap-2">
              <span className={`truncate ${r.featured ? 'text-base font-semibold text-slate-900' : 'text-sm font-medium text-slate-700'}`}>{r.payee}</span>
              <span className="mono-id shrink-0">{r.id}</span>
            </div>
            <div className="text-xs text-slate-500">{r.note}{r.due ? ` · due ${md(r.due)}` : ''}</div>
          </div>
          <div className={`num w-24 shrink-0 text-right ${r.featured ? 'text-base font-semibold text-slate-900' : 'text-sm text-slate-700'}`}>{usd(r.amount_cents)}</div>
          <button onClick={() => pay(r.id)} disabled={busy === r.id} className="btn-primary w-24 shrink-0">
            {busy === r.id ? 'Checking...' : 'Pay'}
          </button>
        </div>
        {d && <ResultCard d={d} flow={flow} onApprove={fix => approveAndExecute(d, fix)} />}
      </li>
    )
  }

  return (
    <section className="card overflow-hidden border-violet-200">
      <header className="flex items-center gap-3 bg-violet-700 px-4 py-3 text-white">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-white font-black text-violet-700">L</div>
        <div>
          <div className="font-semibold leading-tight">Ledgerly AP</div>
          <div className="text-xs text-violet-100">Bills · calls the CashGuard API before paying</div>
        </div>
        <div className="ml-auto text-right text-xs text-violet-100">{state?.company}<br />as of {state?.as_of}</div>
      </header>
      {err && <div className="m-3 rounded-lg border border-block-line bg-block-soft p-2 text-sm text-block-ink">{err}</div>}
      <div className="flex items-center gap-2 border-b border-slate-100 bg-violet-50/60 px-4 py-2">
        <h2 className="text-sm font-semibold text-violet-900">Needs approval</h2>
        <span className="rounded-full bg-violet-100 px-2 text-xs font-medium text-violet-800">{featured.length}</span>
      </div>
      <ul className="divide-y divide-slate-100">
        {featured.map(renderRow)}
        {rows.length === 0 && <li className="px-4 py-6 text-sm text-slate-500">No bills loaded.</li>}
      </ul>
      {others.length > 0 && (
        <details className="border-t border-slate-200">
          <summary className="cursor-pointer select-none bg-slate-50 px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-100">
            Other scheduled bills ({others.length})
          </summary>
          <ul className="divide-y divide-slate-100">{others.map(renderRow)}</ul>
        </details>
      )}
    </section>
  )
}
