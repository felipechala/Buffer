import { usd } from '../api'
import type { TwinStateResponse } from '../types'

export default function TwinStateTab({ state }: { state: TwinStateResponse | null }) {
  if (!state) return <p className="text-sm text-slate-500">Loading twin state...</p>
  return (
    <div className="space-y-6 text-sm">
      <div className="text-xs text-slate-500">
        {state.company} · snapshot <span className="font-mono">{state.snapshot_id}</span> · as of {state.as_of} · {state.horizon_days}-day horizon
      </div>
      <table className="w-full text-left">
        <caption className="mb-1 text-left font-semibold">Accounts</caption>
        <thead className="text-xs uppercase text-slate-500"><tr>
          <th className="py-1">Account</th><th>Role</th><th className="text-right">Balance</th><th className="text-right">Buffer</th><th className="text-right">Headroom</th><th className="pl-4">Source</th>
        </tr></thead>
        <tbody>{state.accounts.map(a => (
          <tr key={a.id} className="border-t border-slate-100">
            <td className="py-1 font-medium">{a.name} <span className="font-mono text-xs text-slate-400">{a.id}</span></td>
            <td>{a.role}</td>
            <td className="text-right font-mono tabular-nums">{usd(a.observed_balance_cents)}</td>
            <td className="text-right font-mono tabular-nums">{usd(a.buffer_cents)}</td>
            <td className={`text-right font-mono tabular-nums ${a.observed_balance_cents < a.buffer_cents ? 'text-red-700' : ''}`}>{usd(a.observed_balance_cents - a.buffer_cents)}</td>
            <td className="pl-4 text-xs text-slate-500">{a.provider} · {a.as_of.slice(0, 10)}</td>
          </tr>))}
        </tbody>
      </table>
      <div className="grid gap-6 md:grid-cols-2">
        <table className="w-full text-left">
          <caption className="mb-1 text-left font-semibold">Obligations ({state.obligations.length})</caption>
          <thead className="text-xs uppercase text-slate-500"><tr><th>Due</th><th>Payee</th><th>Acct</th><th className="text-right">Amount</th></tr></thead>
          <tbody>{state.obligations.map(o => (
            <tr key={o.id} className="border-t border-slate-100">
              <td className="py-0.5 font-mono text-xs">{o.due_date}</td>
              <td>{o.counterparty}{o.nondeferrable && <span className="ml-1 rounded bg-slate-200 px-1 text-[10px]">fixed</span>}</td>
              <td className="text-xs text-slate-500">{o.account_id}</td>
              <td className="text-right font-mono tabular-nums">{usd(o.amount_cents)}</td>
            </tr>))}</tbody>
        </table>
        <table className="w-full text-left">
          <caption className="mb-1 text-left font-semibold">Expected receipts ({state.receipts.length})</caption>
          <thead className="text-xs uppercase text-slate-500"><tr><th>Expected</th><th>From</th><th>Id</th><th className="text-right">Amount</th></tr></thead>
          <tbody>{state.receipts.map(r => (
            <tr key={r.id} className="border-t border-slate-100">
              <td className="py-0.5 font-mono text-xs">{r.expected_date}</td>
              <td>{r.counterparty}</td>
              <td className="font-mono text-xs text-slate-500">{r.id}</td>
              <td className="text-right font-mono tabular-nums">{usd(r.amount_cents)}</td>
            </tr>))}</tbody>
        </table>
      </div>
      <details>
        <summary className="cursor-pointer text-xs text-slate-500">Assumptions &amp; provenance</summary>
        <ul className="mt-1 list-disc pl-5 text-xs text-slate-600">{state.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
        <pre className="mt-1 overflow-auto text-xs text-slate-500">{JSON.stringify(state.provenance, null, 2)}</pre>
      </details>
    </div>
  )
}
