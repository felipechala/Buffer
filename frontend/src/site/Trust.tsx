import { Icon, Reveal, Section } from './shared'

const ITEMS = [
  { icon: 'check', t: 'Verified fixes', d: 'Every fix is replayed in exact cents, then scored on held-out scenarios the optimizer never saw.' },
  { icon: 'sparkle', t: 'Grounded explanations', d: 'Claude writes the explanation, but every dollar amount and date must appear in the results. If one does not, a deterministic template is used instead.' },
  { icon: 'user', t: 'Human approval gate', d: 'A fix is approved by a person, bound to the snapshot it was computed on. If the cash picture changed, approval returns 409.' },
  { icon: 'lock', t: 'Sandbox-gated execution', d: 'Execution is off unless enabled, needs an idempotency key, and today only touches the Nessie sandbox.' },
  { icon: 'log', t: 'Append-only audit trail', d: 'Runs, approvals and executions are recorded in an audit log you can read at /v1/events.' },
  { icon: 'shield', t: 'No fix without proof', d: 'If the solver, the replay or the verifier fails, Buffer never returns an actionable fix. It says why instead.' },
]

export default function Trust() {
  return (
    <Section id="trust" title="The engine decides. The AI explains."
      intro="Buffer is built so that a wrong answer is hard to act on. The decision comes from a deterministic engine; the language model only describes it.">
      <ul className="grid gap-x-10 gap-y-9 md:grid-cols-2 lg:grid-cols-3">
        {ITEMS.map(i => (
          <li key={i.t}>
            <Reveal className="flex gap-4">
              <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-cg-ink text-emerald-300"><Icon name={i.icon} size={20} /></span>
              <div>
                <h3 className="text-[17px] font-semibold">{i.t}</h3>
                <p className="mt-1.5 text-[15px] leading-[1.6] text-slate-600">{i.d}</p>
              </div>
            </Reveal>
          </li>
        ))}
      </ul>
    </Section>
  )
}
