import { useState } from 'react'
import { toCurl, toJs, toPython, type CallRecord } from '../api'
import { useInspector } from '../store'

function JsonNode({ k, v, depth }: { k?: string; v: unknown; depth: number }) {
  const [open, setOpen] = useState(depth < 1)
  const label = k !== undefined ? <span className="text-sky-300">{k}: </span> : null
  if (v === null || typeof v !== 'object') {
    const cls = typeof v === 'string' ? 'text-emerald-300' : typeof v === 'number' ? 'text-amber-300' : 'text-fuchsia-300'
    return <div className="pl-4">{label}<span className={cls}>{JSON.stringify(v)}</span></div>
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
    <div className="mt-2">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">{title}</div>
      <div className="max-h-64 overflow-auto rounded bg-slate-900 p-2 font-mono text-xs text-slate-200">
        {v === null || v === undefined ? <span className="text-slate-500">none</span> : <JsonNode v={v} depth={0} />}
      </div>
    </div>
  )
}

export default function Inspector() {
  const { calls, clear } = useInspector()
  const [selected, setSelected] = useState<number | null>(null)
  const [copied, setCopied] = useState('')
  const cur: CallRecord | undefined = calls.find(c => c.id === selected) ?? calls[0]

  async function copy(label: string, text: string) {
    try { await navigator.clipboard.writeText(text); setCopied(label); setTimeout(() => setCopied(''), 1200) } catch { /* ignore */ }
  }

  return (
    <section className="rounded-xl border border-slate-700 bg-slate-800 p-3 text-slate-100 shadow-sm">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold">CashGuard API inspector</h2>
        <button onClick={clear} className="ml-auto text-xs text-slate-400 hover:text-white">clear</button>
      </div>
      {!cur ? <p className="mt-3 text-sm text-slate-400">No calls yet.</p> : (
        <>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span className="rounded bg-slate-700 px-1.5 py-0.5 font-mono text-xs font-bold">{cur.method}</span>
            <span className="break-all font-mono text-xs">{cur.path}</span>
            <span className={`rounded px-1.5 py-0.5 font-mono text-xs ${cur.status >= 200 && cur.status < 300 ? 'bg-emerald-700' : 'bg-red-700'}`}>{cur.status || 'ERR'}</span>
            <span className="font-mono text-xs text-slate-400">{cur.ms} ms</span>
          </div>
          <div className="mt-2 flex gap-2">
            {([['curl', toCurl], ['Python', toPython], ['JS', toJs]] as const).map(([l, f]) => (
              <button key={l} onClick={() => copy(l, f(cur))}
                className="rounded border border-slate-600 px-2 py-0.5 text-xs hover:bg-slate-700">
                {copied === l ? 'copied ✓' : `copy ${l}`}
              </button>
            ))}
          </div>
          <Json title="Request" v={cur.request} />
          <Json title="Response" v={cur.response} />
        </>
      )}
      <div className="mt-3 text-[11px] font-semibold uppercase tracking-wide text-slate-400">History ({calls.length})</div>
      <ul className="mt-1 max-h-40 overflow-auto text-xs">
        {calls.map(c => (
          <li key={c.id}>
            <button onClick={() => setSelected(c.id)}
              className={`flex w-full items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-slate-700 ${c.id === cur?.id ? 'bg-slate-700' : ''}`}>
              <span className={`w-9 font-mono ${c.status >= 200 && c.status < 300 ? 'text-emerald-300' : 'text-red-300'}`}>{c.status || 'ERR'}</span>
              <span className="w-10 font-mono text-slate-400">{c.method}</span>
              <span className="truncate font-mono">{c.path}</span>
              <span className="ml-auto font-mono text-slate-500">{c.ms}ms</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}
