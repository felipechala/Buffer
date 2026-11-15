import { Icon, Reveal, Section } from './shared'

const QA: [string, string][] = [
  ['Why not just pay everything on its due date?',
    'For a bill you already owe, delaying never lowers your balance, so paying on the due date is usually right. Buffer’s main job is different: checking new spend and payments someone wants to make now, and positioning cash across accounts for obligations that cannot move, like payroll and tax.'],
  ['Does it move money?',
    'Not by default. Buffer returns a decision and fixes. A person approves a fix, and execution is gated, needs an idempotency key, and only reaches the Nessie sandbox. No real money moves in this prototype.'],
  ['What data does it need?',
    'Account balances and buffers, scheduled bills and payroll or tax dates, expected customer receipts, and the settlement lags and credit-line terms for your transfer routes. A connector reads these from your bank or finance system.'],
  ['How is this different from spend-management policies?',
    'Policies answer "is this allowed?" using rules like approval limits and category budgets. Buffer answers "can the accounts absorb this, in combination with everything else coming over the next 14 days?" The two work together: a policy tool can call Buffer before it approves.'],
  ['How accurate is it?',
    'In this prototype, the numbers come from synthetic data and a documented shock model, not from real customers, and they are not calibrated. The evaluation above shows how the method behaves on that synthetic data. Do not read it as a forecast for a real company.'],
  ['Is the AI making decisions?',
    'No. The engine decides allow, warn or block and computes the fixes. Claude only writes the plain-English explanation, and every amount and date in it must appear in the engine’s results, otherwise a deterministic template is shown.'],
]

export default function Faq() {
  return (
    <Section id="faq" tone="canvas" title="Questions people ask.">
      <Reveal className="max-w-[820px] divide-y divide-slate-200 border-y border-slate-200">
        {QA.map(([q, a]) => (
          <details key={q} className="group py-1">
            <summary className="flex cursor-pointer items-center justify-between gap-4 py-4 text-[17px] font-semibold text-cg-ink">
              <span>{q}</span>
              <Icon name="chevron" size={20} className="chev shrink-0 text-slate-500" />
            </summary>
            <p className="pb-5 pr-8 text-[15.5px] leading-[1.7] text-slate-600">{a}</p>
          </details>
        ))}
      </Reveal>
    </Section>
  )
}
