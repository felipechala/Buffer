import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '../api'
import type { Decision, Fix, PurchaseRequest, TwinStateResponse } from '../types'
import Inspector from '../components/Inspector'
import CashStrip from './CashStrip'
import ChatClient from './ChatClient'
import CheckoutClient from './CheckoutClient'
import CardClient from './CardClient'
import { PurchaseCtx, type CommitLogEntry, type CommitResult, type PurchaseApi, type StepState } from './shared'

const TONE = { allow: 'text-allow', warn: 'text-warn', block: 'text-block' } as const
const SCRIPT = [
  { label: 'Card: laptops', expect: 'allow' },
  { label: 'Chat: rush order, pick a fix', expect: 'warn' },
  { label: 'Checkout: CNC machine', expect: 'block' },
  { label: 'Card: trade show', expect: 'warn' },
] as const

const errMsg = (e: unknown) => {
  const t = e instanceof ApiError ? (e.body as { error?: { type?: string } } | null)?.error?.type : null
  return (t ? t + ': ' : '') + (e instanceof Error ? e.message : String(e))
}

export default function PurchasesDemo({ rev, resetKey }: { rev: number; resetKey: number }) {
  const [requests, setRequests] = useState<PurchaseRequest[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [twin, setTwin] = useState<TwinStateResponse | null>(null)
  const [commits, setCommits] = useState<CommitLogEntry[]>([])
  const [steps, setSteps] = useState<Record<string, StepState>>({})

  const loadState = useCallback(async () => {
    try { setTwin(await api.state()) } catch { /* strip just keeps its last values */ }
  }, [])
  // reload on mount and whenever App syncs (Reset demo, source switch)
  useEffect(() => {
    void loadState()
    api.purchasesDemo().then(r => { setRequests([...r.requests].sort((a, b) => a.step - b.step)); setLoadError(null) })
      .catch(e => setLoadError(errMsg(e)))
  }, [rev, loadState])
  useEffect(() => { if (resetKey > 0) { setCommits([]); setSteps({}) } }, [resetKey])

  const check = useCallback(async (req: PurchaseRequest): Promise<Decision> => {
    const d = await api.simulate({
      proposed_action: {
        kind: 'payment', amount_cents: req.amount_cents, from_account: req.from_account, payee: req.payee,
        pay_date: req.pay_date, channel: req.channel, description: req.description, requested_by: req.requested_by,
      },
    })
    setSteps(s => ({ ...s, [req.id]: { decision: d.decision, committed: false } }))
    return d
  }, [])

  const commit = useCallback(async (req: PurchaseRequest, d: Decision, fix: Fix | null): Promise<CommitResult> => {
    try {
      const res = await api.commitPurchase({ run_id: d.run_id, fix_id: fix?.id ?? null, pay_date: null })
      setCommits(c => [...c, { purchaseId: res.purchase_id, reqId: req.id, step: req.step, payee: req.payee_name,
        amount_cents: req.amount_cents, channel: req.channel, scheduled: res.scheduled_date, at: new Date() }])
      setSteps(s => ({ ...s, [req.id]: { decision: d.decision, committed: true } }))
      void loadState()
      return { ok: true, res }
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        try { return { ok: false, stale: true, decision: await check(req) } } catch (e2) { return { ok: false, message: errMsg(e2) } }
      }
      return { ok: false, message: errMsg(e) }
    }
  }, [check, loadState])

  const acctName = useCallback((id: string) => twin?.accounts.find(a => a.id === id)?.name ?? id, [twin])
  const api2: PurchaseApi = useMemo(() => ({ requests, loadError, check, commit, commits, steps, acctName, resetKey }),
    [requests, loadError, check, commit, commits, steps, acctName, resetKey])

  const done = SCRIPT.map((_, i) => requests.find(r => r.step === i + 1)).map(r => (r ? steps[r.id] : undefined))
  const next = done.findIndex(d => !d)

  return (
    <PurchaseCtx.Provider value={api2}>
      <nav aria-label="Demo script" className="mb-3 flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-1.5 shadow-sm">
        <span className="shrink-0 text-xs font-semibold text-slate-700">Demo script</span>
        <ol className="flex min-w-0 flex-1 flex-wrap items-center gap-x-1 gap-y-1">
          {SCRIPT.map((s, i) => {
            const st = done[i]
            return (
              <li key={i} className="flex items-center">
                {i > 0 && <span className="mx-1 text-slate-300" aria-hidden>/</span>}
                <span aria-label={`Step ${i + 1}: ${s.label}${st ? ' (done)' : ''}`}
                  className={`flex items-center gap-1.5 rounded-md px-1.5 py-0.5 text-xs ${i === next ? 'bg-slate-100 font-semibold text-slate-900' : 'text-slate-600'}`}>
                  <span className={`flex h-4 w-4 items-center justify-center rounded-full text-2xs font-bold ${st ? 'bg-allow text-white' : i === next ? 'bg-cg-accent text-white' : 'bg-slate-200 text-slate-600'}`}>{st ? '✓' : i + 1}</span>
                  {s.label}
                  <span className={`font-semibold ${st ? TONE[st.decision] : 'text-slate-400'}`}>{st ? st.decision : s.expect}</span>
                  {st?.committed && <span className="chip chip-ok">committed</span>}
                </span>
              </li>
            )
          })}
        </ol>
      </nav>
      {loadError && (
        <div className="mb-3 rounded-lg border border-block-line bg-block-soft p-2 text-sm text-block-ink">
          Couldn’t load the demo purchase requests: {loadError}. Is the backend up to date?
        </div>
      )}
      <CashStrip state={twin} commits={commits} />
      <div className="grid items-start gap-4 lg:grid-cols-3 min-[1400px]:grid-cols-[repeat(3,minmax(0,1fr))_400px]">
        <div className="h-[640px]"><ChatClient /></div>
        <div className="h-[640px]"><CheckoutClient /></div>
        <div className="h-[640px]"><CardClient /></div>
        <div className="lg:col-span-3 min-[1400px]:col-span-1"><Inspector /></div>
      </div>
    </PurchaseCtx.Provider>
  )
}
