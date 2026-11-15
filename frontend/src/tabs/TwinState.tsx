import { prettyId, usd } from '../api'
import type { TwinStateResponse } from '../types'

const Box = ({ title, aside, children }: { title: string; aside?: React.ReactNode; children: React.ReactNode }) => (
  <section className="rounded-lg border border-slate-200">
    <div className="flex items-baseline gap-2 border-b border-slate-100 px-3 py-2">
      <h3 className="sec-title">{title}</h3>{aside}
    </div>
    <div className="overflow-x-auto px-1 py-1">{children}</div>
  </section>
)

export default function TwinStateTab({ state }: { state: TwinStateResponse | null }) {
  if (!state) return <p className="text-sm text-slate-500">Loading twin state...</p>
  const isPosted = (r: { id: string; posted?: boolean }) => r.posted === true || r.id.startsWith('posted:')
  const receipts = state.receipts.filter(r => !isPosted(r))
  const hiddenPosted = state.receipts.length - receipts.length
  return (
    <div className="space-y-4 text-sm">
      <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
        <span className="font-medium text-slate-700">{state.company}</span>
        <span className="chip chip-neutral font-mono">{state.snapshot_id}</span>
        <span>as of {state.as_of}</span><span>{state.horizon_days}-day horizon</span>
      </div>
      <Box title="Accounts">
        <table className="tbl">
          <thead><tr>
            <th>Account</th><th>Role</th><th className="r">Balance</th><th className="r">Buffer</th><th className="r">Headroom</th><th className="pl-6">Source</th>
          </tr></thead>
          <tbody>{state.accounts.map(a => {
            const head = a.observed_balance_cents - a.buffer_cents
            return (
              <tr key={a.id}>
                <td className="font-medium text-slate-900">{a.name} <span className="mono-id">{a.id}</span></td>
                <td className="text-slate-600">{a.role}</td>
                <td className="r num">{usd(a.observed_balance_cents)}</td>
                <td className="r num text-slate-500">{usd(a.buffer_cents)}</td>
                <td className={`r num font-medium ${head < 0 ? 'text-block' : head === 0 ? 'text-warn' : 'text-slate-900'}`}>{usd(head)}</td>
                <td className="pl-6 text-xs text-slate-500">{a.provider} · {a.as_of.slice(0, 10)}</td>
              </tr>)
          })}</tbody>
        </table>
      </Box>
      <div className="grid items-start gap-4 md:grid-cols-2">
        <Box title={`Obligations (${state.obligations.length})`}>
          <table className="tbl">
            <thead><tr><th>Due</th><th>Payee</th><th>Account</th><th className="r">Amount</th></tr></thead>
            <tbody>{state.obligations.map(o => (
              <tr key={o.id}>
                <td className="num text-xs text-slate-600">{o.due_date}</td>
                <td>{o.payee_name || prettyId(o.counterparty)}{o.nondeferrable && <span className="chip chip-neutral ml-1.5">fixed</span>}</td>
                <td className="mono-id">{o.account_id}</td>
                <td className="r num">{usd(o.amount_cents)}</td>
              </tr>))}</tbody>
          </table>
        </Box>
        <Box title={`Expected receipts (${receipts.length})`} aside={hiddenPosted > 0 ? <span className="text-xs text-slate-500">{hiddenPosted} posted transfer{hiddenPosted > 1 ? 's' : ''} hidden</span> : undefined}>
          <table className="tbl">
            <thead><tr><th>Expected</th><th>From</th><th>Id</th><th className="r">Amount</th></tr></thead>
            <tbody>{receipts.map(r => (
              <tr key={r.id}>
                <td className="num text-xs text-slate-600">{r.expected_date}</td>
                <td className="whitespace-nowrap">{prettyId(r.counterparty)}</td>
                <td className="mono-id max-w-[10rem] truncate whitespace-nowrap" title={r.id}>{r.id}</td>
                <td className="r num whitespace-nowrap">{usd(r.amount_cents)}</td>
              </tr>))}</tbody>
          </table>
        </Box>
      </div>
      <details className="rounded-lg border border-slate-200 px-3 py-2">
        <summary className="cursor-pointer text-xs font-medium text-slate-600">Assumptions &amp; provenance</summary>
        <ul className="mt-2 list-disc pl-5 text-xs text-slate-600">{state.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
        <pre className="mt-2 overflow-auto rounded bg-slate-50 p-2 font-mono text-xs text-slate-600">{JSON.stringify(state.provenance, null, 2)}</pre>
      </details>
    </div>
  )
}
