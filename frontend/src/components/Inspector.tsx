import { useState } from 'react'
import { toCurl, toJs, toPython, type CallRecord } from '../api'
import { useInspector } from '../store'

// Syntax colours: Tailwind 300-level tints on #0b1220 (all > 7:1)
function JsonNode({ k, v, depth }: { k?: string; v: unknown; depth: number }) {
  const [open, setOpen] = useState(depth < 1)
  const label = k !== undefined ? <span className="text-sky-300">{k}<span className="text-slate-500">: </span></span> : null
  if (v === null || typeof v !== 'object') {
    const cls = typeof v === 'string' ? 'text-emerald-300' : typeof v === 'number' ? 'text-amber-300' : 'text-violet-300'
    return <div className="break-words pl-4">{label}<span className={cls}>{JSON.stringify(v)}</span></div>
  }
  const entries = Array.isArray(v) ? v.map((x, i) => [String(i), x] as const) : Object.entries(v as object)
  const brace = Array.isArray(v) ? ['[', ']'] : ['{', '}']
  return (
    <div className="pl-4">
      <button onClick={() => setOpen(!open)} className="text-left hover:text-white">
        <span className="inline-block w-3 text-slate-500">{open ? '▾' : '▸'}</span>{label}
        <span className="text-slate-400">{brace[0]}{open ? '' : ` ${entries.length} ${brace[1]}`}</span>
      </button>
      {open && <>{entries.map(([ek, ev]) => <JsonNode key={ek} k={ek} v={ev} depth={depth + 1} />)}
        <div className="pl-4 text-slate-400">{brace[1]}</div></>}
    </div>
  )
}

function Json({ title, v }: { title: string; v: unknown }) {
  return (
    <div className="mt-3">
      <div className="mb-1 text-xs font-medium text-slate-400">{title}</div>
      <div className="max-h-56 overflow-auto rounded-md border border-white/5 bg-[#0b1220] p-2.5 font-mono text-xs leading-5 text-slate-200">
        {v === null || v === undefined ? <span className="text-slate-500">none</span> : <JsonNode v={v} depth={0} />}
      </div>
    </div>
  )
}

const chan = (r: unknown): string | null =>
  ((r as { proposed_action?: { channel?: string } } | null)?.proposed_action?.channel) ?? null
const CHAN: Record<string, string> = { card: 'bg-teal-400/15 text-teal-300', chat: 'bg-fuchsia-400/15 text-fuchsia-300', checkout: 'bg-orange-400/15 text-orange-300' }
const Chan = ({ c }: { c: string | null }) => c ? <span className={`rounded px-1.5 text-2xs font-semibold ${CHAN[c] ?? 'bg-white/10 text-slate-300'}`}>{c}</span> : null

const ok = (s: number) => s >= 200 && s < 300
const METHOD = { GET: 'bg-sky-400/15 text-sky-300', POST: 'bg-violet-400/20 text-violet-300' } as Record<string, string>

export default function Inspector() {
  const { calls, clear } = useInspector()
  const [selected, setSelected] = useState<number | null>(null)
  const [copied, setCopied] = useState('')
  const cur: CallRecord | undefined = calls.find(c => c.id === selected) ?? calls[0]

  async function copy(label: string, text: string) {
    try { await navigator.clipboard.writeText(text); setCopied(label); setTimeout(() => setCopied(''), 1200) } catch { /* ignore */ }
  }

  return (
    <section className="rounded-xl border border-slate-700 bg-cg-ink2 p-4 text-slate-100 shadow-sm">
      <div className="flex items-center gap-2">
        <h2 className="text-lg font-semibold">CashGuard API inspector</h2>
        <span className="rounded-full bg-white/10 px-2 py-0.5 text-2xs text-slate-300">live</span>
        <button onClick={clear} className="ml-auto rounded px-2 py-0.5 text-xs text-slate-400 hover:bg-white/10 hover:text-white">clear</button>
      </div>
      {!cur ? <p className="mt-3 text-sm text-slate-400">No calls yet. Pay a bill to see the request CashGuard receives.</p> : (
        <>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <span className={`rounded px-1.5 py-0.5 font-mono text-xs font-bold ${METHOD[cur.method] ?? 'bg-white/10 text-slate-200'}`}>{cur.method}</span>
            <span className="break-all font-mono text-xs text-slate-100">{cur.path}</span>
            <span className={`rounded px-1.5 py-0.5 font-mono text-xs font-semibold ${ok(cur.status) ? 'bg-emerald-400/15 text-emerald-300' : 'bg-red-400/20 text-red-300'}`}>{cur.status || 'ERR'}</span>
            <span className="font-mono text-xs text-slate-400">{cur.ms} ms</span>
            <Chan c={chan(cur.request)} />
          </div>
          <div className="mt-2.5 flex gap-2">
            {([['curl', toCurl], ['Python', toPython], ['JS', toJs]] as const).map(([l, f]) => (
              <button key={l} onClick={() => copy(l, f(cur))}
                className="rounded-md border border-white/15 px-2.5 py-1 text-xs text-slate-200 hover:bg-white/10">
                {copied === l ? 'copied ✓' : `copy ${l}`}
              </button>
            ))}
          </div>
          <Json title="Request" v={cur.request} />
          <Json title="Response" v={cur.response} />
        </>
      )}
      <div className="mt-4 mb-1 text-xs font-medium text-slate-400">History ({calls.length}) <span className="font-normal text-slate-500">· click a call to inspect</span></div>
      <ul className="max-h-44 divide-y divide-white/5 overflow-auto rounded-md border border-white/10 text-xs">
        {calls.map(c => (
          <li key={c.id}>
            <button onClick={() => setSelected(c.id)} aria-current={c.id === cur?.id}
              className={`flex w-full items-center gap-2 border-l-2 px-2 py-1.5 text-left hover:bg-white/10 ${c.id === cur?.id ? 'border-sky-400 bg-white/10' : 'border-transparent'}`}>
              <span className={`w-8 font-mono font-semibold ${ok(c.status) ? 'text-emerald-300' : 'text-red-300'}`}>{c.status || 'ERR'}</span>
              <span className={`w-11 rounded text-center font-mono text-2xs font-bold ${METHOD[c.method] ?? 'text-slate-300'}`}>{c.method}</span>
              <span className="truncate font-mono text-slate-200">{c.path}</span>
              <Chan c={chan(c.request)} />
              <span className="ml-auto font-mono text-slate-400">{c.ms}ms</span>
              <span aria-hidden className="text-slate-500">›</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}
