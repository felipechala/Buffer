import { useEffect, useRef, useState, type ReactNode } from 'react'

export const scrollToSection = (id: string) => {
  const el = document.getElementById(id)
  if (!el) return
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' })
}

/** Anchor that scrolls to a section without touching the hash (hash routing stays intact). */
export function SectionLink({ to, children, className = '', onDone }: { to: string; children: ReactNode; className?: string; onDone?: () => void }) {
  return (
    <a href={`#/?s=${to}`} className={className}
      onClick={e => { e.preventDefault(); scrollToSection(to); onDone?.() }}>{children}</a>
  )
}

export function Reveal({ children, className = '' }: { children: ReactNode; className?: string }) {
  const ref = useRef<HTMLDivElement>(null)
  const [shown, setShown] = useState(false)
  useEffect(() => {
    const el = ref.current
    if (!el || typeof IntersectionObserver === 'undefined') { setShown(true); return }
    const io = new IntersectionObserver(es => { if (es.some(e => e.isIntersecting)) { setShown(true); io.disconnect() } }, { threshold: 0.1, rootMargin: '0px 0px -5% 0px' })
    io.observe(el)
    return () => io.disconnect()
  }, [])
  return <div ref={ref} className={`reveal ${shown ? 'in' : ''} ${className}`}>{children}</div>
}

export function Section({ id, title, intro, children, tone = 'light' }: {
  id: string; title: ReactNode; intro?: ReactNode; children: ReactNode; tone?: 'light' | 'canvas' | 'dark'
}) {
  const bg = tone === 'dark' ? 'bg-cg-ink text-slate-300 on-dark' : tone === 'canvas' ? 'bg-canvas' : 'bg-white'
  const h = `${id}-h`
  return (
    <section id={id} aria-labelledby={h} className={`scroll-mt-16 ${bg}`}>
      <div className="mx-auto max-w-[1180px] px-4 py-[72px] sm:px-8 md:py-24">
        <Reveal className="max-w-[720px]">
          <h2 id={h} className={`text-[clamp(28px,4vw,42px)] font-semibold leading-[1.1] ${tone === 'dark' ? '!text-white' : ''}`}>{title}</h2>
          {intro && <p className={`mt-4 text-[17px] leading-[1.65] ${tone === 'dark' ? 'text-slate-300' : 'text-slate-600'}`}>{intro}</p>}
        </Reveal>
        <div className="mt-10 md:mt-14">{children}</div>
      </div>
    </section>
  )
}

const ICONS: Record<string, string[]> = {
  plug: ['M12 22v-5', 'M9 8V2', 'M15 8V2', 'M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z'],
  layers: ['m12 2 10 5-10 5L2 7l10-5Z', 'm2 17 10 5 10-5', 'm2 12 10 5 10-5'],
  zap: ['M13 2 3 14h9l-1 8 10-12h-9l1-8Z'],
  shield: ['M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z', 'm9 12 2 2 4-4'],
  bill: ['M5 3h14v18l-3-2-2 2-2-2-2 2-2-2-3 2V3Z', 'M9 8h6', 'M9 12h6'],
  chat: ['M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2Z'],
  cart: ['M6 6h15l-1.5 9h-12L6 3H2', 'M9 20h.01', 'M18 20h.01'],
  card: ['M2 5h20v14H2z', 'M2 10h20', 'M6 15h4'],
  users: ['M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2', 'M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8', 'M23 21v-2a4 4 0 0 0-3-3.9', 'M16 3.1a4 4 0 0 1 0 7.8'],
  chart: ['M3 3v18h18', 'm7 14 4-4 3 3 5-6'],
  copy: ['M9 9h11v11H9z', 'M5 15H4V4h11v1'],
  check: ['m5 12 5 5 9-10'],
  chevron: ['m6 9 6 6 6-6'],
  menu: ['M4 6h16', 'M4 12h16', 'M4 18h16'],
  x: ['M6 6l12 12', 'M18 6 6 18'],
  eye: ['M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z', 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6'],
  lock: ['M5 11h14v10H5z', 'M8 11V7a4 4 0 0 1 8 0v4'],
  log: ['M4 4h16v16H4z', 'M8 9h8', 'M8 13h8', 'M8 17h4'],
  user: ['M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2', 'M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8'],
  sparkle: ['M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3Z'],
}
export function Icon({ name, size = 20, className = '' }: { name: string; size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">
      {(ICONS[name] ?? []).map((d, i) => <path key={i} d={d} />)}
    </svg>
  )
}

export const useReducedMotion = () => {
  const [r, setR] = useState(() => typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches)
  useEffect(() => {
    const m = window.matchMedia('(prefers-reduced-motion: reduce)')
    const on = () => setR(m.matches)
    m.addEventListener('change', on)
    return () => m.removeEventListener('change', on)
  }, [])
  return r
}

export const DecisionBadge = ({ d, dark = false }: { d: 'allow' | 'warn' | 'block'; dark?: boolean }) => {
  const light = { allow: 'bg-allow-soft text-allow-ink ring-allow-line', warn: 'bg-warn-soft text-warn-ink ring-warn-line', block: 'bg-block-soft text-block-ink ring-block-line' }
  const dk = { allow: 'bg-emerald-400/15 text-emerald-300 ring-emerald-400/40', warn: 'bg-amber-400/15 text-amber-300 ring-amber-400/40', block: 'bg-red-400/15 text-red-300 ring-red-400/40' }
  return <span className={`inline-flex items-center rounded-md px-2 py-0.5 font-mono text-[12px] font-semibold ring-1 ring-inset ${(dark ? dk : light)[d]}`}>{d.toUpperCase()}</span>
}
