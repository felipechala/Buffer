import { useEffect, useMemo, useState } from 'react'
import { useInspector } from '../store'

const KEY = 'cashguard.demoScript.dismissed'
const read = () => { try { return localStorage.getItem(KEY) === '1' } catch { return false } }
const write = (v: boolean) => { try { if (v) localStorage.setItem(KEY, '1'); else localStorage.removeItem(KEY) } catch { /* ignore */ } }

interface Sim { bill_id?: string; decision?: string }

const STEPS = [
  { bill: 'BILL-LEASE-11', label: 'Pay Office Lease', result: 'allow' },
  { bill: 'BILL-NW-4471', label: 'Pay Northwind', result: 'warn' },
  { bill: 'BILL-NW-4471', label: 'Approve a fix, re-check', result: 'allow' },
  { bill: 'BILL-EQUIP-9', label: 'Pay Equipment', result: 'block' },
] as const

const TONE = { allow: 'text-allow', warn: 'text-warn', block: 'text-block' } as const

export default function DemoScript({ resetKey }: { resetKey: number }) {
  const { calls } = useInspector()
  const [dismissed, setDismissed] = useState(read)
  const [baseline, setBaseline] = useState(0)
  // a reset starts the script over: ignore everything recorded before it
  useEffect(() => { if (resetKey > 0) setBaseline(calls[0]?.id ?? 0) }, [resetKey]) // eslint-disable-line react-hooks/exhaustive-deps

  const done = useMemo(() => {
    const chrono = calls.filter(c => c.id > baseline).slice().reverse()
    const sims = chrono.map(c => (c.path.endsWith('/v1/twin/simulations') && c.method === 'POST' ? (c.response as Sim | null) : null))
    const hit = (bill: string, res: string) => sims.some(r => r?.bill_id === bill && r?.decision === res)
    const approveAt = chrono.findIndex(c => /\/approve$/.test(c.path) && c.status >= 200 && c.status < 300)
    const rechecked = approveAt >= 0 && sims.slice(approveAt).some(r => r?.bill_id === 'BILL-NW-4471' && r?.decision === 'allow')
    return [hit('BILL-LEASE-11', 'allow'), hit('BILL-NW-4471', 'warn'), rechecked, hit('BILL-EQUIP-9', 'block')]
  }, [calls, baseline])

  if (dismissed) {
    return <button onClick={() => { write(false); setDismissed(false) }} className="mb-3 text-xs text-slate-500 underline-offset-2 hover:text-slate-800 hover:underline">Show demo script</button>
  }
  const next = done.findIndex(d => !d)
  const go = (bill: string) => document.getElementById('bill-' + bill)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  return (
    <nav aria-label="Demo script" className="mb-3 flex flex-wrap items-start gap-x-3 gap-y-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 shadow-sm sm:flex-nowrap sm:items-center">
      <span className="order-1 shrink-0 py-0.5 text-xs font-semibold text-slate-700">Demo script</span>
      <ol className="order-3 flex w-full min-w-0 flex-wrap items-center gap-x-3 gap-y-1 sm:order-2 sm:w-auto sm:flex-1">
        {STEPS.map((s, i) => (
          <li key={i} className="flex items-center">
                        <button onClick={() => go(s.bill)} aria-label={`Step ${i + 1}: ${s.label}${done[i] ? ' (done)' : ''}`}
              className={`flex items-center gap-1.5 rounded-md px-1.5 py-0.5 text-xs hover:bg-slate-100 ${i === next ? 'bg-slate-100 font-semibold text-slate-900' : 'text-slate-600'}`}>
              <span className={`flex h-4 w-4 items-center justify-center rounded-full text-2xs font-bold ${done[i] ? 'bg-allow text-white' : i === next ? 'bg-brand text-white' : 'bg-slate-200 text-slate-600'}`}>
                {done[i] ? '✓' : i + 1}
              </span>
              {s.label}
              <span className={`font-semibold ${done[i] ? TONE[s.result] : 'text-slate-400'}`}>{s.result}</span>
            </button>
          </li>
        ))}
      </ol>
      <button onClick={() => { write(true); setDismissed(true) }} aria-label="Dismiss demo script" className="order-2 ml-auto shrink-0 rounded p-1 text-slate-400 sm:order-3 sm:ml-0 hover:bg-slate-100 hover:text-slate-700">✕</button>
    </nav>
  )
}
