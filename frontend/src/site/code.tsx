import { useState, type ReactNode } from 'react'
import { Icon } from './shared'

const RX = /("(?:[^"\\]|\\.)*")(\s*:)?|(\b-?\d+(?:\.\d+)?\b)|\b(true|false|null|import|from|const|await|print|curl|requests|json|def|return|fetch|headers|body|method)\b|(#.*$|\/\/.*$)|(\$[A-Z_]+)/g
const DECISION: Record<string, string> = { '"allow"': 'text-emerald-300', '"warn"': 'text-amber-300', '"block"': 'text-red-300' }

/** Tiny single-line highlighter for JSON / shell / python / js snippets. */
export function hl(line: string): ReactNode {
  const out: ReactNode[] = []
  let last = 0, i = 0
  for (const m of line.matchAll(RX)) {
    const at = m.index ?? 0
    if (at > last) out.push(<span key={i++} className="tk-p">{line.slice(last, at)}</span>)
    const [txt, str, colon, num, kw, com, env] = m
    if (str) {
      if (colon) { out.push(<span key={i++} className="tk-k">{str}</span>); out.push(<span key={i++} className="tk-p">{colon}</span>) }
      else out.push(<span key={i++} className={`${DECISION[str] ?? 'tk-s'} ${DECISION[str] ? 'font-semibold' : ''}`}>{str}</span>)
    } else if (num) out.push(<span key={i++} className="tk-n">{num}</span>)
    else if (kw) out.push(<span key={i++} className="tk-k">{kw}</span>)
    else if (com) out.push(<span key={i++} className="tk-c">{com}</span>)
    else if (env) out.push(<span key={i++} className="tk-n">{env}</span>)
    else out.push(<span key={i++}>{txt}</span>)
    last = at + txt.length
  }
  if (last < line.length) out.push(<span key={i++} className="tk-p">{line.slice(last)}</span>)
  return out
}

export function CopyButton({ text, label = 'Copy' }: { text: string; label?: string }) {
  const [done, setDone] = useState(false)
  return (
    <button type="button" aria-label={`${label} to clipboard`}
      onClick={() => {
        const ok = () => { setDone(true); setTimeout(() => setDone(false), 1600) }
        if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text).then(ok, () => undefined)
      }}
      className="inline-flex items-center gap-1.5 rounded-md border border-white/15 px-2.5 py-1 text-[12px] font-medium text-slate-200 hover:bg-white/10">
      <Icon name={done ? 'check' : 'copy'} size={14} />
      <span aria-live="polite">{done ? 'Copied' : label}</span>
    </button>
  )
}
