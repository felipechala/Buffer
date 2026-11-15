import { DEMO_LABEL, EXT, PURCHASES_HREF } from "./links"
import { useEffect, useState } from 'react'
import { hl } from './code'
import { DecisionBadge, SectionLink, useReducedMotion } from './shared'

type D = 'allow' | 'warn' | 'block'
const EXAMPLES: { id: string; label: string; d: D; req: string[]; res: string[]; ms: number }[] = [
  {
    id: 'laptops', label: 'Laptops, $5,000', d: 'allow', ms: 41,
    req: ['{', '  "proposed_action": {', '    "payee": "dell",', '    "from_account": "operating",', '    "amount_cents": 500000,', '    "pay_date": "2026-11-20"', '  }', '}'],
    res: ['{', '  "decision": "allow",', '  "safe": true,', '  "breaks": [],', '  "fixes": [],', '  "latency_ms": 41', '}'],
  },
  {
    id: 'northwind', label: 'Northwind rush, $30,000', d: 'warn', ms: 64,
    req: ['{', '  "proposed_action": {', '    "payee": "northwind",', '    "from_account": "operating",', '    "amount_cents": 3000000,', '    "pay_date": "2026-11-20"', '  }', '}'],
    res: ['{', '  "decision": "warn",', '  "breaks": [', '    { "date": "2026-11-24", "account": "payroll",', '      "shortfall_cents": 2800000 }', '  ],', '  "fixes": [', '    { "summary": "Wait until Nov 25",', '      "cost_cents": 0, "verified": true }', '  ],', '  "latency_ms": 64', '}'],
  },
  {
    id: 'cnc', label: 'CNC machine, $450,000', d: 'block', ms: 88,
    req: ['{', '  "proposed_action": {', '    "payee": "haas",', '    "from_account": "operating",', '    "amount_cents": 45000000,', '    "pay_date": "2026-11-20"', '  }', '}'],
    res: ['{', '  "decision": "block",', '  "safe": false,', '  "breaks": [ ... ],', '  "fixes": [],', '  "constraint_report": [', '    "No date or funding route is verified safe." ],', '  "latency_ms": 88', '}'],
  },
]
const HOLD = 3600, STEP = 150

export default function Hero() {
  const reduced = useReducedMotion()
  const [idx, setIdx] = useState(1)
  const [auto, setAuto] = useState(true)
  const [shown, setShown] = useState(99)
  const ex = EXAMPLES[idx]

  useEffect(() => {
    if (reduced || !auto) { setShown(99); return }
    setShown(0)
    let n = 0
    const t = setInterval(() => {
      n += 1; setShown(n)
      if (n >= ex.res.length) clearInterval(t)
    }, STEP)
    const next = setTimeout(() => setIdx(i => (i + 1) % EXAMPLES.length), STEP * ex.res.length + HOLD)
    return () => { clearInterval(t); clearTimeout(next) }
  }, [idx, auto, reduced, ex.res.length])

  const pick = (i: number) => { setAuto(false); setIdx(i); setShown(99) }
  const done = shown >= ex.res.length

  return (
    <section aria-labelledby="hero-h" className="hero-bg on-dark relative overflow-hidden">
      <div className="mx-auto grid max-w-[1180px] items-center gap-12 px-4 pb-16 pt-14 sm:px-8 md:pb-24 md:pt-20 lg:grid-cols-[minmax(0,1fr)_minmax(0,560px)] lg:gap-14">
        <div>
          <h1 id="hero-h" className="text-[clamp(44px,7vw,76px)] font-semibold leading-[1.02] tracking-[-0.035em] !text-white">
            Ask before<br className="hidden sm:block" /> you pay.
          </h1>
          <p className="mt-6 max-w-[520px] text-[18px] leading-[1.6] text-slate-300">
            Stripe Radar stops fraud before a payment goes through. Buffer stops a cash crunch before a payment goes through.
          </p>
          <p className="mt-4 max-w-[520px] text-[15px] leading-[1.6] text-slate-400">
            One API call from your payroll, bill-pay, purchasing or card app. It checks the payment against every account for the next 14 days and answers allow, warn or block.
          </p>
          <div className="mt-9 flex flex-wrap gap-3">
            <a href={PURCHASES_HREF} {...EXT} className="rounded-lg bg-brand px-5 py-3 text-[15px] font-semibold text-white shadow-[0_8px_24px_-8px_rgba(72,64,255,.8)] hover:bg-brand-ink">{DEMO_LABEL}</a>
            <SectionLink to="api" className="rounded-lg border border-white/25 px-5 py-3 text-[15px] font-semibold text-white hover:bg-white/10">Read the API</SectionLink>
          </div>
        </div>

        <div className="min-w-0" role="group" aria-label="Example API calls">
          <div className="mb-3 flex flex-wrap gap-2" role="tablist" aria-label="Example payments">
            {EXAMPLES.map((e, i) => (
              <button key={e.id} role="tab" aria-selected={i === idx} onClick={() => pick(i)}
                className={`rounded-full border px-3 py-1 text-[13px] font-medium ${i === idx ? 'border-white/60 bg-white/15 text-white' : 'border-white/15 text-slate-300 hover:bg-white/10'}`}>
                {e.label}
              </button>
            ))}
          </div>

          <div className="overflow-hidden rounded-xl border border-white/12 bg-[#0a1626] shadow-[0_30px_80px_-30px_rgba(0,0,0,.8)]" style={{ borderColor: 'rgba(255,255,255,.12)' }}>
            <div className="flex items-center justify-between gap-3 border-b border-white/10 bg-white/[.04] px-4 py-2.5">
              <p className="code min-w-0 truncate text-slate-200"><span className="font-semibold text-emerald-300">POST</span> /v1/twin/simulations</p>
              <span className="hidden shrink-0 font-mono text-[11px] text-slate-400 sm:inline">Authorization: Bearer cg_test_...</span>
            </div>
            <div className="grid px-4 py-3.5">
              {EXAMPLES.map((e, i) => (
                <pre key={e.id} aria-hidden={i !== idx} className={`code col-start-1 row-start-1 overflow-hidden ${i === idx ? '' : 'invisible'}`}>
                  {e.req.map((l, k) => <div key={k}>{hl(l)}</div>)}
                </pre>
              ))}
            </div>
            <div className="flex items-center justify-between border-y border-white/10 bg-white/[.04] px-4 py-2.5">
              <p className="code text-slate-300"><span className="text-slate-400">response</span> 200</p>
              <div className="flex h-6 items-center" aria-live="polite">
                {done ? <DecisionBadge d={ex.d} dark /> : <span className="code text-slate-400">simulating<span className="caret" aria-hidden /></span>}
              </div>
            </div>
            <div className="grid px-4 py-3.5">
              {EXAMPLES.map((e, i) => (
                <pre key={e.id} aria-hidden={i !== idx} className={`code col-start-1 row-start-1 overflow-hidden ${i === idx ? '' : 'invisible'}`}>
                  {e.res.map((l, k) => <div key={k} className={i === idx && k < shown ? (reduced ? '' : 'line-in') : 'opacity-0'}>{hl(l)}</div>)}
                </pre>
              ))}
            </div>
          </div>
          <p className="mt-3 text-[13px] leading-5 text-slate-400">Abbreviated, illustrative responses. Amounts are in integer cents.</p>
        </div>
      </div>
    </section>
  )
}
