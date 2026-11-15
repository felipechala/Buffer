import { Icon, Reveal, Section } from './shared'

const STEPS = [
  { icon: 'plug', t: 'Connect', d: 'Read balances, scheduled bills and expected customer receipts from your bank or finance connector.' },
  { icon: 'layers', t: 'Twin', d: 'Build a 14-day projection across every account: buffers, payroll and tax, transfer settlement lags and credit-line delays.' },
  { icon: 'zap', t: 'Stress', d: 'An adaptive search hunts for plausible shocks: customers paying late, a transfer route down, a surprise outflow.' },
  { icon: 'shield', t: 'Decide', d: 'Return allow, warn or block, what breaks and when, the cheapest verified fixes, and a plain-English explanation.' },
]

const APPS = ['Bill pay and AP', 'Procurement and checkout', 'Card authorization', 'Payroll', 'Chat ops']
const ENGINE = [['Twin', '14-day multi-account projection'], ['Stress', 'adaptive search for shocks'], ['Decide', 'allow / warn / block + verified fixes'], ['Explain', 'Claude, grounded in the results']]

function Arrow({ x1, y1, x2, y2 }: { x1: number; y1: number; x2: number; y2: number }) {
  const mx = (x1 + x2) / 2
  return <path d={`M${x1} ${y1} C${mx} ${y1} ${mx} ${y2} ${x2} ${y2}`} fill="none" stroke="#94a3b8" strokeWidth="1.5" markerEnd="url(#ah)" />
}

function Architecture() {
  const ys = [44, 108, 172, 236, 300]
  return (
    <svg viewBox="0 0 960 380" role="img" className="hidden h-auto w-full md:block"
      aria-label="Architecture: many client apps call one Buffer API. The API runs the twin, stress search, decision and explanation layers, and reads from finance connectors.">
      <defs>
        <marker id="ah" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 8 4 0 8z" fill="#94a3b8" /></marker>
      </defs>
      <text x="20" y="22" fontSize="13" fontWeight="600" fill="#475569">Apps that move money</text>
      <text x="340" y="22" fontSize="13" fontWeight="600" fill="#475569">One shared cash twin</text>
      <text x="740" y="22" fontSize="13" fontWeight="600" fill="#475569">Your finance stack</text>
      {APPS.map((a, i) => (
        <g key={a}>
          <rect x="20" y={ys[i]} width="220" height="48" rx="10" fill="#fff" stroke="#cbd5e1" />
          <text x="40" y={ys[i] + 29} fontSize="14" fontWeight="500" fill="#0b1b2e">{a}</text>
          <Arrow x1={240} y1={ys[i] + 24} x2={338} y2={198} />
        </g>
      ))}
      <rect x="340" y="40" width="290" height="312" rx="14" fill="#0b1b2e" />
      <rect x="360" y="52" width="86" height="26" rx="6" fill="#4840FF" />
      <text x="403" y="70" textAnchor="middle" fontSize="12.5" fontWeight="700" fill="#fff" fontFamily="JetBrains Mono, monospace">/v1 API</text>
      {ENGINE.map(([t, d], i) => (
        <g key={t}>
          <rect x="360" y={92 + i * 62} width="250" height="52" rx="9" fill="#13263d" stroke="rgba(255,255,255,.14)" />
          <text x="376" y={92 + i * 62 + 22} fontSize="14" fontWeight="600" fill="#fff">{t}</text>
          <text x="376" y={92 + i * 62 + 40} fontSize="12" fill="#cbd5e1">{d}</text>
        </g>
      ))}
      <Arrow x1={630} y1={150} x2={738} y2={86} />
      <Arrow x1={630} y1={196} x2={738} y2={196} />
      <Arrow x1={630} y1={242} x2={738} y2={306} />
      <rect x="740" y="60" width="200" height="52" rx="10" fill="#fff" stroke="#cbd5e1" />
      <text x="756" y="83" fontSize="14" fontWeight="600" fill="#0b1b2e">Bank connector</text>
      <text x="756" y="101" fontSize="12" fill="#475569">Capital One Nessie sandbox</text>
      <rect x="740" y="170" width="200" height="52" rx="10" fill="#fff" stroke="#cbd5e1" strokeDasharray="5 4" />
      <text x="756" y="193" fontSize="14" fontWeight="600" fill="#0b1b2e">More connectors</text>
      <text x="756" y="211" fontSize="12" fill="#475569">on the roadmap</text>
      <rect x="740" y="280" width="200" height="52" rx="10" fill="#fff" stroke="#cbd5e1" />
      <text x="756" y="303" fontSize="14" fontWeight="600" fill="#0b1b2e">Audit log</text>
      <text x="756" y="321" fontSize="12" fill="#475569">append-only, every decision</text>
    </svg>
  )
}

function ArchitectureMobile() {
  const box = 'rounded-lg border border-slate-300 bg-white px-3.5 py-2.5 text-[14px] font-medium text-cg-ink'
  const down = <div className="my-1.5 text-center text-slate-400" aria-hidden>&darr;</div>
  return (
    <div className="md:hidden" role="group" aria-label="Architecture, top to bottom">
      <p className="mb-2 text-[13px] font-semibold text-slate-600">Apps that move money</p>
      <div className="grid grid-cols-2 gap-2">{APPS.map(a => <div key={a} className={box}>{a}</div>)}</div>
      {down}
      <div className="rounded-xl bg-cg-ink p-3.5">
        <p className="mb-2 inline-block rounded bg-brand px-2 py-0.5 font-mono text-[12px] font-bold text-white">/v1 API</p>
        <div className="space-y-2">{ENGINE.map(([t, d]) => <div key={t} className="rounded-lg border border-white/15 bg-cg-ink2 px-3 py-2"><p className="text-[14px] font-semibold text-white">{t}</p><p className="text-[12.5px] text-slate-300">{d}</p></div>)}</div>
      </div>
      {down}
      <p className="mb-2 text-[13px] font-semibold text-slate-600">Your finance stack</p>
      <div className="space-y-2"><div className={box}>Bank connector: Capital One Nessie sandbox</div><div className={`${box} border-dashed`}>More connectors on the roadmap</div></div>
    </div>
  )
}

export default function HowItWorks() {
  return (
    <Section id="how" title="Four steps between a request and an answer."
      intro="Buffer sits next to the button that moves money. Your app sends the payment it is about to make and gets back a decision it can act on. Decisions come from the CashGuard engine behind the Buffer API.">
      <ol className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        {STEPS.map((s, i) => (
          <li key={s.t}>
            <Reveal className="h-full">
              <div className="h-full rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
                <div className="flex items-center justify-between">
                  <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-brand-soft text-brand"><Icon name={s.icon} size={22} /></span>
                  <span className="font-mono text-[13px] font-medium text-slate-500" aria-label={`Step ${i + 1}`}>{i + 1}/4</span>
                </div>
                <h3 className="mt-4 text-[19px] font-semibold">{s.t}</h3>
                <p className="mt-2 text-[15px] leading-[1.6] text-slate-600">{s.d}</p>
              </div>
            </Reveal>
          </li>
        ))}
      </ol>
      <Reveal className="mt-10 rounded-2xl border border-slate-200 bg-canvas p-5 sm:p-8">
        <h3 className="mb-5 text-[17px] font-semibold">Many apps, one API, one cash twin</h3>
        <Architecture />
        <ArchitectureMobile />
        <p className="mt-5 max-w-[680px] text-[15px] leading-[1.6] text-slate-600">
          Because every app asks the same twin, a purchase approved in one app changes the answer in another.
        </p>
      </Reveal>
    </Section>
  )
}
