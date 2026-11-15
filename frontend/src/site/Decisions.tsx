import { Reveal, Section } from './shared'

const CARDS = [
  { d: 'allow', t: 'Allow', tone: 'border-allow-line bg-allow-soft', ink: 'text-allow-ink', dot: 'bg-allow',
    rule: 'The requested date needs no actions. No breach in the baseline or the stress set, and the held-out breach rate is within your risk tolerance.',
    app: 'Proceed with the payment.' },
  { d: 'warn', t: 'Warn', tone: 'border-warn-line bg-warn-soft', ink: 'text-warn-ink', dot: 'bg-warn',
    rule: 'Something breaches, in the baseline or under a stress scenario, and at least one verified fix exists. Also used when a plan is safe only with residual risk.',
    app: 'Show the reason and the fixes. A person chooses.' },
  { d: 'block', t: 'Block', tone: 'border-block-line bg-block-soft', ink: 'text-block-ink', dot: 'bg-block',
    rule: 'The baseline breaches and no fix exists on any legal date within your constraints. The response says why.',
    app: 'Hold the payment and show the explanation.' },
]

export default function Decisions() {
  return (
    <Section id="decisions" tone="canvas" title="One answer your app can act on."
      intro="Every simulation returns allow, warn or block. A breach that shows up only under a hypothetical shock, with no fix, is a warn with an explanation. Buffer does not block on a maybe.">
      <div className="grid gap-4 md:grid-cols-3">
        {CARDS.map(c => (
          <Reveal key={c.d} className="h-full">
            <article className={`flex h-full flex-col rounded-xl border p-6 ${c.tone}`}>
              <div className="flex items-center gap-2.5">
                <span className={`h-2.5 w-2.5 rounded-full ${c.dot}`} aria-hidden />
                <h3 className={`font-mono text-[18px] font-semibold !tracking-normal ${c.ink}`}>{c.d}</h3>
              </div>
              <p className="mb-5 mt-4 text-[15px] leading-[1.6] text-slate-700">{c.rule}</p>
              <div className="mt-auto border-t border-black/10 pt-4">
                <p className="text-[13px] font-medium text-slate-600">Your app should</p>
                <p className={`mt-1 text-[16px] font-semibold ${c.ink}`}>{c.app}</p>
              </div>
            </article>
          </Reveal>
        ))}
      </div>
    </Section>
  )
}
