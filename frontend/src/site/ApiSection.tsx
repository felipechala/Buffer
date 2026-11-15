import { useState } from 'react'
import { DOCS_HREF, DOCS_LABEL, EXT } from './links'
import { CopyButton, hl } from './code'
import { Reveal, Section } from './shared'

const REQ_JSON = `{
  "proposed_action": {
    "kind": "payment",
    "payee": "Northwind Supply",
    "amount_cents": 3000000,
    "pay_date": "2026-11-20",
    "channel": "bill_pay"
  },
  "horizon_days": 14,
  "risk_tolerance": 0.05
}`

const SAMPLES: Record<'curl' | 'Python' | 'JavaScript', string> = {
  curl: `curl -X POST https://YOUR_HOST/v1/twin/simulations \\
  -H "Authorization: Bearer $CASHGUARD_KEY" \\
  -H "Content-Type: application/json" \\
  -d '${REQ_JSON}'`,
  Python: `import os, requests

resp = requests.post(
    "https://YOUR_HOST/v1/twin/simulations",
    headers={"Authorization": f"Bearer {os.environ['CASHGUARD_KEY']}"},
    json={
        "proposed_action": {
            "kind": "payment",
            "payee": "Northwind Supply",
            "amount_cents": 3000000,
            "pay_date": "2026-11-20",
            "channel": "bill_pay",
        },
        "horizon_days": 14,
        "risk_tolerance": 0.05,
    },
)
decision = resp.json()
print(decision["decision"], decision["explanation"])`,
  JavaScript: `const res = await fetch("https://YOUR_HOST/v1/twin/simulations", {
  method: "POST",
  headers: {
    "Authorization": \`Bearer \${process.env.CASHGUARD_KEY}\`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    proposed_action: {
      kind: "payment",
      payee: "Northwind Supply",
      amount_cents: 3000000,
      pay_date: "2026-11-20",
      channel: "bill_pay",
    },
    horizon_days: 14,
    risk_tolerance: 0.05,
  }),
});
const decision = await res.json();
console.log(decision.decision, decision.explanation);`,
}

const RESPONSE = `{
  "run_id": "run_7c1a...",
  "decision": "warn",
  "safe": false,
  "breaks": [{
    "date": "2026-11-24",
    "account": "payroll",
    "obligation": "Biweekly payroll",
    "shortfall_cents": 2800000,
    "scenario": { "id": "acme_late_3d", "label": "Acme invoice arrives 3 days late", "plausibility": 0.6 }
  }],
  "fixes": [{
    "summary": "Pay Northwind on Nov 25 instead of Nov 20",
    "pay_date": "2026-11-25",
    "cost_cents": 0,
    "verified": true,
    "held_out": { "breach_rate": 0.0, "paths": 200 }
  }],
  "explanation": "Paying $30,000 on Nov 20 is safe if Acme pays on time, but if its invoice is 3 days late, payroll on Nov 24 is short $28,000. Paying on Nov 25 avoids this at no cost.",
  "explanation_url": "/v1/runs/run_7c1a.../explanation",
  "provenance": {
    "snapshot_id": "snap_9f2c...",
    "input_digest": "sha256:...",
    "engine_version": "optimizer-lp-0.2",
    "scenario_seed": 42,
    "source": "nessie"
  },
  "latency_ms": 64
}`

const ENDPOINTS: [string, string, string][] = [
  ['GET', '/v1/connections', 'Connector health, mode and last sync'],
  ['POST', '/v1/connections/{provider}/sync', 'Read the connector, build a new snapshot (nessie or fixture)'],
  ['GET', '/v1/twin/state', 'Reconciled accounts, obligations, receipts, snapshot id'],
  ['POST', '/v1/twin/forecasts', '14-day projection per account, with stressed curves'],
  ['POST', '/v1/twin/simulations', 'Check a proposed payment: decision, breaks, verified fixes'],
  ['POST', '/v1/twin/stress-tests', 'Start the adaptive risk search (202); poll GET /v1/twin/stress-tests/{id}'],
  ['POST', '/v1/plans/{id}/approve', 'Human approval bound to the snapshot; 409 if state changed'],
  ['POST', '/v1/plans/{id}/execute-sandbox', 'Gated sandbox execution; Idempotency-Key required'],
  ['POST', '/v1/purchases/commit', 'Commit a checked purchase so every other app sees it'],
  ['GET', '/v1/events', 'Append-only audit log of runs, approvals and executions'],
]

const HABITS: [string, string][] = [
  ['Bearer auth', 'Every /v1 call carries an Authorization header.'],
  ['Idempotency keys', 'Approve, execute and commit accept an Idempotency-Key. A repeat returns the stored result, never a second write.'],
  ['409 on stale snapshots', 'If the cash picture changed since a fix was computed, approval is refused and you re-run the check.'],
  ['Provenance on every response', 'Snapshot id, input digest, engine version, seed and data source.'],
  ['Async explanation', 'The decision returns immediately. The written explanation is fetched from explanation_url.'],
  ['One error envelope', 'Errors share one shape with a type, a message and the parameter at fault.'],
]

const TABS = ['curl', 'Python', 'JavaScript'] as const

function CodeBlock({ title, text, right }: { title: string; text: string; right?: React.ReactNode }) {
  return (
    <div className="h-full overflow-hidden rounded-xl border border-white/10 bg-[#0a1626] shadow-lg">
      <div className="flex items-center justify-between gap-3 border-b border-white/10 bg-white/[.04] px-4 py-2">
        <div className="min-w-0 flex-1">{right ?? <span className="code text-slate-300">{title}</span>}</div>
        <CopyButton text={text} />
      </div>
      <pre tabIndex={0} aria-label={title} className="code overflow-auto whitespace-pre-wrap break-words px-4 py-4">
        {text.split('\n').map((l, i) => <div key={i} className="whitespace-pre-wrap">{hl(l) || ' '}</div>)}
      </pre>
    </div>
  )
}

export default function ApiSection() {
  const [tab, setTab] = useState<typeof TABS[number]>('curl')
  return (
    <Section id="api" tone="dark" title="One call before money moves."
      intro="POST the payment you are about to make. The response has everything your app needs to allow it, hold it, or offer a fix.">
      <div className="grid gap-6 lg:grid-cols-2">
        <Reveal className="min-w-0">
          <CodeBlock title={`Request (${tab})`} text={SAMPLES[tab]} right={
            <div role="tablist" aria-label="Language" className="flex gap-1">
              {TABS.map(t => (
                <button key={t} role="tab" aria-selected={t === tab} onClick={() => setTab(t)}
                  className={`rounded-md px-2.5 py-1 text-[13px] font-medium ${t === tab ? 'bg-white/15 text-white' : 'text-slate-300 hover:bg-white/10'}`}>{t}</button>
              ))}
            </div>} />
        </Reveal>
        <Reveal className="min-w-0">
          <CodeBlock title="Response (abbreviated)" text={RESPONSE} />
        </Reveal>
      </div>
      <p className="mt-3 text-[13px] text-slate-400">Illustrative values on the synthetic demo company. Amounts are integer cents; dates are ISO.</p>

      <div className="mt-16 grid gap-12 lg:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)]">
        <Reveal className="min-w-0">
          <h3 className="mb-4 text-[20px] font-semibold !text-white">Endpoints</h3>
          <div className="overflow-hidden rounded-xl border border-white/10">
            <table className="w-full text-left">
              <caption className="sr-only">Buffer v1 endpoints</caption>
              <thead className="sr-only"><tr><th>Method</th><th>Path</th><th>Purpose</th></tr></thead>
              <tbody>
                {ENDPOINTS.map(([m, p, d]) => (
                  <tr key={p + m} className="border-b border-white/10 last:border-0 align-top max-sm:block max-sm:px-4 max-sm:py-3">
                    <td className="whitespace-nowrap py-3 pl-4 pr-2 font-mono text-[12px] font-semibold text-emerald-300 max-sm:inline max-sm:p-0 max-sm:pr-2">{m}</td>
                    <td className="py-3 pr-4 font-mono text-[12.5px] text-slate-100 max-sm:inline max-sm:p-0 max-sm:break-all sm:whitespace-nowrap">{p}</td>
                    <td className="py-3 pr-4 text-[14px] leading-[1.5] text-slate-300 max-sm:mt-1 max-sm:block max-sm:p-0">{d}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-4 text-[14px] text-slate-300">
            Full schema: <a className="font-medium text-blue-300 underline underline-offset-4 hover:text-white" href={DOCS_HREF} {...EXT}>{DOCS_LABEL}</a>
          </p>
        </Reveal>
        <Reveal className="min-w-0">
          <h3 className="mb-4 text-[20px] font-semibold !text-white">Built for production habits</h3>
          <ul className="space-y-4">
            {HABITS.map(([t, d]) => (
              <li key={t} className="flex gap-3">
                <span className="mt-1 text-emerald-300" aria-hidden>&#10003;</span>
                <p className="text-[15px] leading-[1.55] text-slate-300"><strong className="font-semibold text-white">{t}.</strong> {d}</p>
              </li>
            ))}
          </ul>
        </Reveal>
      </div>
    </Section>
  )
}
