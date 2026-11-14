import { useState } from 'react'
import { api, usd } from '../api'
import type { Decision, Fix, TwinStateResponse } from '../types'
import ResultCard, { type FixFlowState } from './ResultCard'

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
    return { id: b.id, payee: o?.counterparty ?? b.payee, note: b.note, amount_cents: o?.amount_cents ?? DEMO_AMOUNTS[b.id],
             due: o?.due_date, featured: true }
  })
  for (const o of state?.obligations ?? []) {
    if (!DEMO_BILLS.some(b => b.id === o.id))
      rows.push({ id: o.id, payee: o.counterparty, note: o.category, amount_cents: o.amount_cents, due: o.due_date, featured: false })
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

  return (
    <section className="rounded-xl border border-violet-200 bg-white shadow-sm">
      <header className="flex items-center gap-3 rounded-t-xl bg-violet-700 px-4 py-3 text-white">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-white font-black text-violet-700">L</div>
        <div>
          <div className="font-semibold leading-tight">Ledgerly AP</div>
          <div className="text-xs text-violet-200">Bills · powered by CashGuard API</div>
        </div>
        <div className="ml-auto text-right text-xs text-violet-200">{state?.company}<br />as of {state?.as_of}</div>
      </header>
      {err && <div className="m-3 rounded bg-red-50 p-2 text-sm text-red-700">{err}</div>}
      <ul className="divide-y divide-slate-100">
        {rows.map(r => {
          const d = results[r.id]
          return (
            <li key={r.id} className={`px-4 py-3 ${r.featured ? '' : 'opacity-80'}`}>
              <div className="flex items-center gap-3">
                <div className="min-w-0 flex-1">
                  <div className="truncate font-medium text-slate-800">{r.payee} <span className="font-mono text-xs text-slate-400">{r.id}</span></div>
                  <div className="text-xs text-slate-500">{r.note}{r.due ? ` · due ${r.due}` : ''}</div>
                </div>
                <div className="font-mono text-sm tabular-nums text-slate-800">{usd(r.amount_cents)}</div>
                <button onClick={() => pay(r.id)} disabled={busy === r.id}
                  className="rounded-md bg-violet-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-violet-700 disabled:opacity-50">
                  {busy === r.id ? 'Checking...' : 'Pay'}
                </button>
              </div>
              {d && <ResultCard d={d} flow={flow} onApprove={fix => approveAndExecute(d, fix)} />}
            </li>
          )
        })}
        {rows.length === 0 && <li className="px-4 py-6 text-sm text-slate-500">No bills loaded.</li>}
      </ul>
    </section>
  )
}
