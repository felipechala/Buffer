import { BILLS_HREF, EXT, PURCHASES_HREF, STATIC } from './links'
import { Icon, Reveal, Section } from './shared'

type Demo = 'bills' | 'purchases' | null
const CASES: { icon: string; t: string; s: string; demo: Demo }[] = [
  { icon: 'bill', t: 'Bill pay and AP payment runs', s: 'Before a run is released, check each bill against payroll, tax and expected receipts, and pay on the date that holds up.', demo: 'bills' },
  { icon: 'chat', t: 'Purchase requests in chat', s: 'A teammate asks for a $30k rush order in chat. The bot checks cash first and replies with a decision and the cheapest fix.', demo: 'purchases' },
  { icon: 'cart', t: 'Procurement and checkout', s: 'A purchase order or checkout flow asks Buffer before committing, so large orders do not collide with obligations that cannot move.', demo: 'purchases' },
  { icon: 'card', t: 'Corporate card authorization', s: 'When a card issuer sees a large authorization, it can ask whether the company’s accounts can absorb it over the next 14 days.', demo: 'purchases' },
  { icon: 'users', t: 'Payroll runs', s: 'Confirm the payroll account holds its buffer through the pay date, including when a customer pays late or a transfer route is down.', demo: 'bills' },
  { icon: 'chart', t: 'Treasury and finance ops', s: 'Run stress tests and 14-day forecasts for the whole company, and read the ranked list of shocks that hurt most.', demo: 'bills' },
]

export default function UseCases() {
  return (
    <Section id="use-cases" title="Wherever money is about to move."
      intro="Buffer is called from the tools where payments and purchases happen. Two live demos show it from different clients on the same data.">
      <ul className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {CASES.map(c => (
          <li key={c.t}>
            <Reveal className="h-full">
              <article className="flex h-full flex-col rounded-xl border border-slate-200 bg-white p-5 shadow-sm transition-shadow hover:shadow-md">
                <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-slate-100 text-cg-ink"><Icon name={c.icon} size={22} /></span>
                <h3 className="mt-4 text-[18px] font-semibold leading-snug">{c.t}</h3>
                <p className="mt-2 flex-1 text-[15px] leading-[1.6] text-slate-600">{c.s}</p>
                {c.demo ? (
                  <a href={c.demo === 'bills' ? BILLS_HREF : PURCHASES_HREF} {...EXT} className="mt-4 text-[14px] font-semibold text-blue-700 underline decoration-blue-300 underline-offset-4 hover:decoration-blue-700">
                    {c.demo === 'bills' ? (STATIC ? 'Run the bill-pay demo' : 'See it in the bill-pay demo') : (STATIC ? 'Run the purchase-checks demo' : 'See it in the purchase-checks demo')}
                  </a>
                ) : null}
              </article>
            </Reveal>
          </li>
        ))}
      </ul>
    </Section>
  )
}
