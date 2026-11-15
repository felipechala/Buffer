import { useEffect, useRef, useState } from 'react'
import type { Decision, Fix, PurchaseRequest } from '../types'
import { usd } from '../api'
import { md } from '../components/ResultCard'
import { clockShort, DecisionPill, Dots, ExplText, fixLabel, Shield, topBreak, useExplanation, usePurchases } from './shared'

type Msg =
  | { id: number; kind: 'user'; text: string; at: Date }
  | { id: number; kind: 'text'; text: string; at: Date; tone?: 'ok' | 'info' }
  | { id: number; kind: 'check'; req: PurchaseRequest; d: Decision | null; at: Date; busy: boolean; resolved: boolean }

function Avatar({ bot, name }: { bot?: boolean; name?: string }) {
  if (bot) return (
    <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-cg-ink">
      <svg width="18" height="18" viewBox="0 0 36 36" aria-hidden><path d="M18 5l11 4.200v8c0 6.500-4.500 11.200-11 14.300-6.500-3.100-11-7.800-11-14.300v-8z" fill="#2563eb" /><path d="M13 18l3.500 3.500 6.500-7" fill="none" stroke="#6ee7b7" strokeWidth="2.600" strokeLinecap="round" strokeLinejoin="round" /></svg>
    </span>
  )
  return <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-[#e8c9a0] text-xs font-bold text-[#5b3a12]">{(name ?? 'P')[0]}</span>
}

function Head({ name, at, bot }: { name: string; at: Date; bot?: boolean }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-sm font-bold text-slate-900">{name}</span>
      {bot && <span className="rounded bg-slate-200 px-1 text-[10px] font-semibold text-slate-600">APP</span>}
      <span className="text-2xs text-slate-500">{clockShort(at)}</span>
    </div>
  )
}

function CheckCard({ m, onFix, onApprove, onEscalate }: {
  m: Extract<Msg, { kind: 'check' }>
  onFix: (m: Msg & { kind: 'check' }, f: Fix) => void
  onApprove: (m: Msg & { kind: 'check' }) => void
  onEscalate: (m: Msg & { kind: 'check' }) => void
}) {
  const { acctName } = usePurchases()
  const d = m.d
  const ex = useExplanation(d)
  if (!d) return (
    <div className="flex items-center gap-2 text-sm text-slate-500"><Dots /> checking cash safety…</div>
  )
  const reason = d.decision === 'allow' ? null : topBreak(d)
  const off = m.busy || m.resolved
  return (
    <div className="mt-1 overflow-hidden rounded-md border border-slate-300 border-l-4 bg-white" style={{ borderLeftColor: d.decision === 'allow' ? '#047857' : d.decision === 'warn' ? '#b45309' : '#b91c1c' }}>
      <div className="space-y-2 px-3 py-2">
        <div className="flex items-center gap-2">
          <DecisionPill level={d.decision} />
          <span className="text-sm font-semibold text-slate-900">{m.req.payee_name} · {usd(m.req.amount_cents)}</span>
          <span className="ml-auto font-mono text-2xs text-slate-500">{d.latency_ms} ms</span>
        </div>
        <ExplText text={ex.text} loading={ex.loading} />
        {reason && <p className="rounded bg-slate-100 px-2 py-1 text-xs text-slate-700">{reason}</p>}
        {d.decision === 'allow' && (
          <button disabled={off} onClick={() => onApprove(m)} className="w-full rounded-md bg-[#007a5a] px-3 py-1.5 text-sm font-semibold text-white hover:bg-[#006048] disabled:opacity-50">Approve</button>
        )}
        {d.decision === 'warn' && (
          <div className="flex flex-col gap-1.5">
            {d.fixes.map(f => (
              <button key={f.id} disabled={off || !f.verified} onClick={() => onFix(m, f)}
                className="rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-left text-sm font-medium text-slate-800 hover:bg-slate-50 disabled:opacity-50">
                {fixLabel(f, acctName)}
              </button>
            ))}
            <button disabled={off} onClick={() => onEscalate(m)}
              className="rounded-md border border-slate-200 px-2.5 py-1.5 text-left text-sm text-slate-600 hover:bg-slate-50 disabled:opacity-50">Escalate to finance</button>
          </div>
        )}
        {d.decision === 'block' && (
          <button disabled={off} onClick={() => onEscalate(m)} className="rounded-md border border-slate-300 px-2.5 py-1.5 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50">Escalate to finance</button>
        )}
      </div>
    </div>
  )
}

export default function ChatClient() {
  const { requests, check, commit, resetKey } = usePurchases()
  const req = requests.find(r => r.channel === 'chat')
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [text, setText] = useState('')
  const nid = useRef(1)
  const scroller = useRef<HTMLDivElement>(null)
  useEffect(() => { setMsgs([]); setText('') }, [resetKey])
  useEffect(() => { scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: 'smooth' }) }, [msgs])

  const suggestion = req ? `Rush order from ${req.payee_name}, ${usd(req.amount_cents)}, today` : ''
  const patch = (id: number, p: Partial<Extract<Msg, { kind: 'check' }>>) =>
    setMsgs(ms => ms.map(m => (m.id === id && m.kind === 'check' ? { ...m, ...p } : m)))
  const say = (t: string, tone?: 'ok' | 'info') => setMsgs(ms => [...ms, { id: nid.current++, kind: 'text', text: t, at: new Date(), tone }])

  async function runCheck(r: PurchaseRequest, id: number) {
    try { patch(id, { d: await check(r), busy: false }) } catch (e) {
      setMsgs(ms => ms.filter(m => m.id !== id)); say(`I couldn't reach Buffer: ${e instanceof Error ? e.message : String(e)}`, 'info')
    }
  }
  async function send() {
    if (!req || !text.trim()) return
    const now = new Date()
    const id = nid.current++
    setMsgs(ms => [...ms, { id: nid.current++, kind: 'user', text: text.trim(), at: now }, { id, kind: 'check', req, d: null, at: now, busy: true, resolved: false }])
    setText('')
    await runCheck(req, id)
  }
  async function doCommit(m: Extract<Msg, { kind: 'check' }>, fix: Fix | null) {
    if (!m.d) return
    patch(m.id, { busy: true })
    const r = await commit(m.req, m.d, fix)
    if (r.ok) {
      patch(m.id, { busy: false, resolved: true })
      say(`Approved — scheduled for ${md(r.res.scheduled_date)}`, 'ok')
    } else if (r.stale) {
      patch(m.id, { d: r.decision, busy: false })
      say('Cash picture changed — re-checked. Please pick again.', 'info')
    } else {
      patch(m.id, { busy: false }); say(`Couldn't commit: ${r.message}`, 'info')
    }
  }

  return (
    <section aria-label="Huddle chat client" className="flex h-full flex-col overflow-hidden rounded-xl border border-slate-300 bg-white shadow-sm">
      <div className="flex items-center gap-2 bg-[#2b1a47] px-3 py-2 text-white">
        <span className="flex h-6 w-6 items-center justify-center rounded-md bg-[#f2b8a2] text-xs font-extrabold text-[#2b1a47]">H</span>
        <span className="text-sm font-bold">Huddle</span>
        <span className="ml-auto text-2xs text-white/60">Acme Corp</span>
      </div>
      <div className="flex items-center gap-2 border-b border-slate-200 px-3 py-1.5">
        <span className="whitespace-nowrap text-sm font-bold text-slate-900"># purchasing</span>
        <Shield className="ml-auto whitespace-nowrap text-slate-500" />
      </div>
      <div ref={scroller} className="min-h-0 flex-1 space-y-3 overflow-auto px-3 py-3">
        {msgs.length === 0 && (
          <p className="rounded-md bg-slate-50 px-3 py-2 text-xs text-slate-500">No messages yet. The Buffer app answers purchase requests posted here before anyone commits the money.</p>
        )}
        {msgs.map(m => (
          <div key={m.id} className="flex gap-2">
            <Avatar bot={m.kind !== 'user'} name={req?.requested_by.replace(/^\W+/, '')} />
            <div className="min-w-0 flex-1">
              {m.kind === 'user' && <><Head name={req?.requested_by ?? 'Priya'} at={m.at} /><p className="text-sm text-slate-800">{m.text}</p></>}
              {m.kind === 'text' && <><Head name="Buffer" at={m.at} bot /><p className={`text-sm ${m.tone === 'ok' ? 'font-semibold text-allow-ink' : 'text-slate-700'}`}>{m.tone === 'ok' && '✓ '}{m.text}</p></>}
              {m.kind === 'check' && <>
                <Head name="Buffer" at={m.at} bot />
                <CheckCard m={m} onFix={(mm, f) => void doCommit(mm, f)} onApprove={mm => void doCommit(mm, null)}
                  onEscalate={mm => { patch(mm.id, { resolved: true }); say('Escalated to #finance — Dana will review the request.', 'info') }} />
              </>}
            </div>
          </div>
        ))}
      </div>
      <div className="border-t border-slate-200 p-2.5">
        {req && !text && (
          <button onClick={() => setText(suggestion)} className="mb-2 max-w-full truncate rounded-full border border-[#2b1a47]/30 bg-[#f6f0fb] px-3 py-1 text-xs font-medium text-[#2b1a47] hover:bg-[#ede2f7]">
            Suggested: {suggestion}
          </button>
        )}
        <form onSubmit={e => { e.preventDefault(); void send() }} className="flex items-center gap-2 rounded-lg border border-slate-300 px-2 py-1 focus-within:border-[#2b1a47]">
          <input value={text} onChange={e => setText(e.target.value)} placeholder="Message #purchasing" aria-label="Message #purchasing"
            className="min-w-0 flex-1 bg-transparent py-1 text-sm outline-none" />
          <button type="submit" disabled={!text.trim() || !req} className="rounded-md bg-[#007a5a] px-2.5 py-1 text-xs font-semibold text-white disabled:opacity-40">Send</button>
        </form>
      </div>
    </section>
  )
}
