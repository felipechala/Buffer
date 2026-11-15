import { Reveal, Section } from './shared'

const X = (d: number) => 40 + (d - 19) * 90
const DAYS = [19, 20, 21, 22, 23, 24, 25, 26]

function Timeline() {
  const expected = `M${X(19)} 120 H${X(20)} V150 H${X(22)} V62 H${X(24)} V110 H${X(26) + 20}`
  const actual = `M${X(19)} 120 H${X(20)} V150 H${X(24)} V198 H${X(25)} V80 H${X(26) + 20}`
  return (
    <svg viewBox="0 0 740 290" role="img" className="h-auto w-full min-w-[620px]"
      aria-label="Schematic of one account's cash from Nov 19 to Nov 26. Paying Northwind on Nov 20 is fine alone. If Acme's invoice arrives three days late, payroll on Nov 24 leaves the account $28 thousand below its buffer.">
      {/* day grid */}
      {DAYS.map(d => (
        <g key={d}>
          <line x1={X(d)} x2={X(d)} y1="24" y2="232" stroke="#e2e8f0" />
          <text x={X(d)} y="256" textAnchor="middle" fontSize="12" fill="#475569" fontFamily="JetBrains Mono, monospace">Nov {d}</text>
        </g>
      ))}
      {/* buffer */}
      <line x1="20" x2="720" y1="170" y2="170" stroke="#b45309" strokeWidth="1.5" strokeDasharray="6 5" />
      <text x="24" y="162" fontSize="12" fill="#92400e" fontWeight="600">buffer</text>
      {/* expected path */}
      <path d={expected} fill="none" stroke="#94a3b8" strokeWidth="2" strokeDasharray="2 5" strokeLinecap="round" />
      <text x={X(22) + 6} y="52" fontSize="12" fill="#475569">as planned: Acme pays on time</text>
      {/* actual */}
      <path d={actual} fill="none" stroke="#0b1b2e" strokeWidth="2.5" strokeLinejoin="round" />
      {/* shortfall */}
      <rect x={X(24) - 1} y="170" width="2" height="28" fill="#b91c1c" />
      <circle cx={X(24)} cy="198" r="5.5" fill="#b91c1c" stroke="#fff" strokeWidth="2" />
      <text x={X(24) - 12} y="216" textAnchor="end" fontSize="13" fontWeight="700" fill="#991b1b">payroll short $28k</text>
      {/* event markers */}
      <circle cx={X(20)} cy="150" r="5" fill="#2563eb" stroke="#fff" strokeWidth="2" />
      <text x={X(20) + 10} y="140" fontSize="12.5" fontWeight="600" fill="#0b1b2e">Northwind $30k paid</text>
      <circle cx={X(25)} cy="80" r="5" fill="#2563eb" stroke="#fff" strokeWidth="2" />
      <text x={X(25) + 10} y="72" fontSize="12.5" fontWeight="600" fill="#0b1b2e">Acme $220k lands</text>
    </svg>
  )
}

export default function Problem() {
  const steps: [string, string][] = [
    ['Nov 20', 'Northwind, $30k. The balance covers it, so the approval screen says yes.'],
    ['Nov 22 to Nov 25', 'Acme’s $220k invoice was expected Nov 22. It arrives 3 days late, on Nov 25.'],
    ['Nov 24', 'Payroll runs while the money is still in transit. The account ends $28k short.'],
  ]
  return (
    <Section id="problem" tone="canvas"
      title="Every payment looks fine on its own."
      intro="Payments are approved one at a time. Cash problems come from how they combine: a customer who pays late, a transfer that takes two days to settle, and payroll on a fixed date.">
      <div className="grid grid-cols-[minmax(0,1fr)] gap-10 lg:grid-cols-[minmax(0,380px)_minmax(0,1fr)] lg:gap-14">
        <Reveal>
          <ol className="relative space-y-7 border-l border-slate-300 pl-6">
            {steps.map(([when, what]) => (
              <li key={when} className="relative">
                <span className="absolute -left-[31px] top-1.5 h-2.5 w-2.5 rounded-full bg-cg-accent ring-4 ring-canvas" aria-hidden />
                <p className="font-mono text-[13px] font-medium text-slate-600">{when}</p>
                <p className="mt-1 text-[16px] leading-[1.55] text-slate-800">{what}</p>
              </li>
            ))}
          </ol>
          <p className="mt-8 rounded-lg border border-block-line bg-block-soft px-4 py-3 text-[15px] font-medium leading-6 text-block-ink">
            A person approving Northwind on Nov 20 never sees Nov 24. Buffer does.
          </p>
        </Reveal>
        <Reveal>
          <figure className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
            <div className="overflow-x-auto"><Timeline /></div>
            <figcaption className="mt-2 text-[13px] leading-5 text-slate-600">
              Schematic of one account&rsquo;s cash, not to scale. Solid line: Acme pays late. Dotted line: as planned.
            </figcaption>
          </figure>
        </Reveal>
      </div>
    </Section>
  )
}
