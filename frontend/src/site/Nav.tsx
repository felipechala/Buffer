import { useEffect, useState } from 'react'
import { Icon, Logo, SectionLink } from './shared'

const LINKS: [string, string][] = [['how', 'How it works'], ['use-cases', 'Use cases'], ['api', 'API'], ['evaluation', 'Evaluation'], ['faq', 'FAQ']]

export default function Nav() {
  const [open, setOpen] = useState(false)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  const link = 'rounded-md px-3 py-2 text-[14px] font-medium text-slate-200 hover:bg-white/10 hover:text-white'
  return (
    <header className="sticky top-0 z-50 border-b border-white/10 bg-cg-ink/90 backdrop-blur on-dark">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-2 focus:rounded focus:bg-white focus:px-3 focus:py-1.5 focus:text-cg-ink"
        onClick={e => { e.preventDefault(); document.getElementById('main')?.focus() }}>Skip to content</a>
      <div className="mx-auto flex h-16 max-w-[1180px] items-center gap-6 px-4 sm:px-8">
        <a href="#/" className="flex items-center gap-2.5 text-white" aria-label="Buffer home"
          onClick={() => window.scrollTo({ top: 0 })}>
          <Logo size={30} />
          <span className="text-[18px] font-bold tracking-tight">Buffer</span>
        </a>
        <nav aria-label="Primary" className="ml-4 hidden items-center gap-1 lg:flex">
          {LINKS.map(([id, l]) => <SectionLink key={id} to={id} className={link}>{l}</SectionLink>)}
        </nav>
        <div className="ml-auto hidden items-center gap-2 sm:flex">
          <a href="/docs" className="rounded-md border border-white/20 px-3.5 py-2 text-[14px] font-medium text-slate-100 hover:bg-white/10">API docs</a>
          <a href="#/purchases" className="rounded-md bg-cg-accent px-3.5 py-2 text-[14px] font-semibold text-white hover:bg-blue-500">Live demo</a>
        </div>
        <button type="button" className="rounded-md p-2 text-white hover:bg-white/10 max-sm:ml-auto lg:hidden" aria-expanded={open} aria-controls="mobile-menu"
          aria-label={open ? 'Close menu' : 'Open menu'} onClick={() => setOpen(o => !o)}>
          <Icon name={open ? 'x' : 'menu'} size={22} />
        </button>
      </div>
      {open && (
        <div id="mobile-menu" className="border-t border-white/10 bg-cg-ink px-4 pb-5 pt-3 lg:hidden">
          <nav aria-label="Mobile" className="flex flex-col">
            {LINKS.map(([id, l]) => <SectionLink key={id} to={id} className={`${link} py-3 text-[16px]`} onDone={() => setOpen(false)}>{l}</SectionLink>)}
          </nav>
          <div className="mt-3 flex gap-2">
            <a href="/docs" className="flex-1 rounded-md border border-white/20 px-3 py-2.5 text-center text-[14px] font-medium text-white">API docs</a>
            <a href="#/purchases" className="flex-1 rounded-md bg-cg-accent px-3 py-2.5 text-center text-[14px] font-semibold text-white">Live demo</a>
          </div>
        </div>
      )}
    </header>
  )
}
