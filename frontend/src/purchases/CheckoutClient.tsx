import { useEffect, useState } from 'react'
import type { Decision, Fix } from '../types'
import { usd } from '../api'
import { md } from '../components/ResultCard'
import { DecisionPill, Dots, ExplText, fixLabel, Shield, topBreak, useExplanation, usePurchases } from './shared'

type Phase = 'idle' | 'checking' | 'result' | 'placing' | 'placed'

function addDays(iso: string, n: number) {
  const d = new Date(iso + 'T00:00:00'); d.setDate(d.getDate() + n)
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

export default function CheckoutClient() {
  const { requests, check, commit, resetKey, acctName } = usePurchases()
  const req = requests.find(r => r.channel === 'checkout')
  const [phase, setPhase] = useState<Phase>('idle')
  const [d, setD] = useState<Decision | null>(null)
  const [pick, setPick] = useState(0)
  const [note, setNote] = useState<string | null>(null)
  const [placed, setPlaced] = useState<string | null>(null)
  const [financing, setFinancing] = useState(false)
  const ex = useExplanation(d)
  useEffect(() => { setPhase('idle'); setD(null); setPick(0); setNote(null); setPlaced(null); setFinancing(false) }, [resetKey])

  if (!req) return <section className="flex h-full items-center justify-center rounded-xl border border-slate-300 bg-white text-sm text-slate-500">Loading storefront…</section>

  async function place() {
    if (!req) return
    setPhase('checking'); setNote(null); setFinancing(false)
    try {
      const dec = await check(req)
      setD(dec); setPick(0)
      if (dec.decision === 'allow') await finish(dec, null)
      else setPhase('result')
    } catch (e) { setNote(e instanceof Error ? e.message : String(e)); setPhase('idle') }
  }
  async function finish(dec: Decision, fix: Fix | null) {
    if (!req) return
    setPhase('placing')
    const r = await commit(req, dec, fix)
    if (r.ok) { setPlaced(r.res.scheduled_date); setPhase('placed') }
    else if (r.stale) { setD(r.decision); setPick(0); setNote('Cash picture changed — re-checked.'); setPhase('result') }
    else { setNote(r.message); setPhase('result') }
  }

  const busy = phase === 'checking' || phase === 'placing'
  const blocked = phase === 'result' && d?.decision === 'block'
  const reason = d && d.decision !== 'allow' ? topBreak(d) : null
  return (
    <section aria-label="Supplier checkout client" className="flex h-full flex-col overflow-hidden rounded-xl border border-slate-300 bg-white shadow-sm">
      <div className="flex items-center gap-2 bg-neutral-900 px-3 py-2 text-white">
        <span className="h-5 w-1.5 bg-orange-500" aria-hidden />
        <span className="truncate text-sm font-extrabold tracking-tight">{req.payee_name}</span>
        <span className="ml-auto shrink-0 text-2xs text-white/60">Order portal</span>
      </div>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto overflow-x-hidden bg-neutral-50 px-3 py-3">
        <div className="rounded-lg border border-neutral-200 bg-white p-3">
          <div className="flex h-24 items-center justify-center rounded bg-neutral-100">
            <svg width="96" height="64" viewBox="0 0 96 64" aria-hidden>
              <rect x="8" y="10" width="64" height="42" rx="3" fill="#d4d4d4" /><rect x="16" y="18" width="30" height="26" fill="#737373" />
              <rect x="52" y="18" width="14" height="26" fill="#f97316" /><rect x="4" y="52" width="80" height="6" fill="#404040" />
            </svg>
          </div>
          <h3 className="mt-2 text-base font-bold leading-5 text-neutral-900">{req.description}</h3>
          <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
            <dt className="text-neutral-500">Quantity</dt><dd className="text-right font-medium">1</dd>
            <dt className="text-neutral-500">Delivery</dt><dd className="text-right font-medium">est. {addDays(req.pay_date, 14)}</dd>
            <dt className="text-neutral-500">Payment</dt><dd className="text-right font-medium">Pay on order</dd>
            <dt className="pt-1 text-sm font-semibold text-neutral-900">Total</dt><dd className="num pt-1 text-right text-lg font-bold text-neutral-900">{usd(req.amount_cents)}</dd>
          </dl>
        </div>

        {phase === 'placed' ? (
          <div className="rounded-lg border border-allow-line bg-allow-soft p-3 text-sm text-allow-ink" role="status">
            <div className="font-bold">✓ Order placed</div>
            <p className="mt-0.5 text-xs">Payment scheduled for {placed ? md(placed) : 'the selected date'}. {d?.decision === 'allow' ? 'Cleared by CashGuard.' : 'Chosen option applied.'}</p>
          </div>
        ) : (
          <button onClick={() => void place()} disabled={busy || blocked || phase === 'result'}
            className="w-full rounded-md bg-orange-600 px-3 py-2 text-sm font-bold text-white hover:bg-orange-700 disabled:opacity-40">
            Place order
          </button>
        )}
        {note && <p className="rounded bg-warn-soft px-2 py-1 text-xs text-warn-ink">{note}</p>}

        {phase === 'checking' && (
          <div className="flex items-center gap-2 text-xs text-neutral-600"><Dots className="bg-orange-500" /> Checking cash safety with CashGuard…</div>
        )}

        {d && (phase === 'result' || phase === 'placing') && (
          <div className={`space-y-2 rounded-lg border bg-white p-3 ${d.decision === 'warn' ? 'border-warn-line' : 'border-block-line'}`}>
            <div className="flex items-start gap-2">
              <DecisionPill level={d.decision} />
              <span className="min-w-0 flex-1 text-sm font-semibold leading-5 text-neutral-900">{d.decision === 'block' ? 'Order can’t be placed' : 'Choose how to pay'}</span>
              <span className="shrink-0 whitespace-nowrap pt-0.5 font-mono text-2xs text-neutral-500">{d.latency_ms} ms</span>
            </div>
            <ExplText text={ex.text} loading={ex.loading} tone="text-neutral-700" />
            {reason && <p className="rounded bg-neutral-100 px-2 py-1 text-xs text-neutral-700">{reason}</p>}

            {d.decision === 'warn' && (
              <fieldset className="space-y-1.5" disabled={busy}>
                <legend className="sr-only">Payment options</legend>
                {d.fixes.map((f, i) => (
                  <label key={f.id} className={`flex cursor-pointer items-start gap-2 rounded-md border px-2.5 py-2 text-sm ${pick === i ? 'border-orange-500 bg-orange-50' : 'border-neutral-200 hover:bg-neutral-50'}`}>
                    <input type="radio" name="fix" checked={pick === i} onChange={() => setPick(i)} className="mt-1 accent-orange-600" />
                    <span className="font-medium text-neutral-800">{fixLabel(f, acctName)}</span>
                  </label>
                ))}
                <button disabled={busy || !d.fixes[pick]} onClick={() => void finish(d, d.fixes[pick])}
                  className="w-full rounded-md bg-orange-600 px-3 py-2 text-sm font-bold text-white hover:bg-orange-700 disabled:opacity-40">
                  {phase === 'placing' ? 'Placing order…' : 'Place order with this option'}
                </button>
              </fieldset>
            )}

            {d.decision === 'block' && (
              <>
                {(d.infeasibility.length > 0 || d.constraint_report.length > 0) && (
                  <details className="rounded-md border border-neutral-200 text-xs">
                    <summary className="cursor-pointer select-none px-2 py-1.5 font-medium text-neutral-700">Why no option exists</summary>
                    <ul className="list-disc space-y-0.5 border-t border-neutral-100 py-2 pl-6 pr-2 text-neutral-600">
                      {d.infeasibility.slice(0, 3).map((v, i) => <li key={'v' + i}>{v.explanations[0] ?? `${v.account_id} short ${usd(v.shortfall_cents)}`}</li>)}
                      {d.constraint_report.slice(0, 5).map((r, i) => <li key={i}>{r}</li>)}
                    </ul>
                  </details>
                )}
                <button onClick={() => setFinancing(true)} disabled={financing}
                  className="w-full rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium text-neutral-800 hover:bg-neutral-50 disabled:opacity-60">
                  {financing ? '✓ Financing review requested' : 'Request financing review'}
                </button>
              </>
            )}
            <Shield className="text-neutral-500" />
          </div>
        )}
      </div>
    </section>
  )
}
