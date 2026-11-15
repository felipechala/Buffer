import { Reveal, Section } from './shared'

const CALLERS: [string, string][] = [
  ['Spend management platforms', 'Add a forward cash check to policy and budget rules (for example tools like Ramp or Brex).'],
  ['AP and bill-pay tools', 'Check a payment run before it is released.'],
  ['Procurement', 'Check a purchase order before it is committed.'],
  ['Chat ops', 'Answer "can we afford this?" inside the thread.'],
  ['Card issuers', 'Check a large authorization against the next 14 days.'],
  ['Payroll', 'Protect the payroll account through the pay date.'],
]

export default function Stack() {
  return (
    <Section id="stack" title="Fits the stack you already run."
      intro="CashGuard complements spend-management tools. They check policy and budget. CashGuard adds a forward cash check they can call before they approve.">
      <div className="grid gap-10 lg:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
        <Reveal>
          <h3 className="mb-4 text-[18px] font-semibold">Apps that call it</h3>
          <ul className="grid gap-3 sm:grid-cols-2">
            {CALLERS.map(([t, d]) => (
              <li key={t} className="rounded-xl border border-slate-200 bg-white p-4">
                <p className="text-[15px] font-semibold text-cg-ink">{t}</p>
                <p className="mt-1 text-[14px] leading-[1.55] text-slate-600">{d}</p>
              </li>
            ))}
          </ul>
        </Reveal>
        <Reveal>
          <h3 className="mb-4 text-[18px] font-semibold">Where it reads from</h3>
          <ul className="divide-y divide-slate-200 rounded-xl border border-slate-200 bg-white">
            <li className="flex items-center justify-between gap-3 p-4">
              <div><p className="text-[15px] font-semibold text-cg-ink">Capital One Nessie</p><p className="text-[14px] text-slate-600">Sandbox connector, read and gated sandbox writes</p></div>
              <span className="chip chip-ok">Available today</span>
            </li>
            {['Plaid', 'QuickBooks', 'Stripe'].map(n => (
              <li key={n} className="flex items-center justify-between gap-3 p-4">
                <p className="text-[15px] font-semibold text-cg-ink">{n}</p>
                <span className="chip chip-neutral">Planned</span>
              </li>
            ))}
          </ul>
          <p className="mt-3 text-[13px] text-slate-600">Connectors sit behind one interface, so a new source does not change the API.</p>
        </Reveal>
      </div>
    </Section>
  )
}
