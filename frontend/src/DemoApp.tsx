import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from './api'
import type { ConnectionsResponse, TwinStateResponse } from './types'
import MockClient from './components/MockClient'
import Inspector from './components/Inspector'
import DemoScript from './components/DemoScript'
import ModeBadge from './components/ModeBadge'
import TwinStateTab from './tabs/TwinState'
import ForecastTab from './tabs/Forecast'
import RiskLabTab from './tabs/RiskLab'
import EventsTab from './tabs/Events'
import PurchasesDemo from './purchases/PurchasesDemo'

const TABS = ['TwinState', 'Forecast', 'RiskLab', 'Events'] as const
type Tab = typeof TABS[number]

export default function DemoApp({ route }: { route: 'bills' | 'purchases' }) {
  const [conn, setConn] = useState<ConnectionsResponse | null>(null)
  const [state, setState] = useState<TwinStateResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('TwinState')
  const [rev, setRev] = useState(0) // bumps whenever the twin snapshot may have changed
  const [resetKey, setResetKey] = useState(0) // bumps on Reset demo (restarts the demo script)

  const refresh = useCallback(async () => {
    try {
      setConn(await api.connections())
      let s: TwinStateResponse
      try { s = await api.state() } catch (e) {
        if (e instanceof ApiError && e.status !== 401) { await api.syncFixture(); s = await api.state() } else throw e
      }
      setState(s); setError(null); setRev(r => r + 1)
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }, [])

  const firstRoute = useRef(true)
  useEffect(() => { if (firstRoute.current) { firstRoute.current = false; return } void refresh() }, [route, refresh]) // purchases commit new snapshots; re-sync Demo 1 view

  const booted = useRef(false) // StrictMode double-invokes effects in dev; bootstrap only once
  useEffect(() => { if (booted.current) return; booted.current = true; void refresh() }, [refresh])
  const [switching, setSwitching] = useState<'fixture' | 'nessie' | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  async function switchSource(src: 'fixture' | 'nessie') {
    setSwitching(src); setNotice(null); setError(null)
    try {
      if (src === 'fixture') await api.syncFixture(); else await api.syncNessie()
      await refresh()
    } catch (e) {
      setNotice(src === 'nessie'
        ? `Nessie sync failed (${e instanceof ApiError ? 'HTTP ' + e.status : 'network'}): ${e instanceof Error ? e.message : String(e)}. Staying on the fixture data.`
        : (e instanceof Error ? e.message : String(e)))
      await refresh()
    } finally { setSwitching(null) }
  }

  async function resetDemo() {
    setResetKey(k => k + 1)
    try { await api.syncFixture(true); await refresh() } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }

  return (
    <div className="mx-auto max-w-[1500px] px-4 py-4">
      <header className="mb-3 flex flex-wrap items-center gap-x-6 gap-y-3 rounded-xl bg-cg-ink px-5 py-3.5 text-white shadow-sm">
        <div className="flex items-center gap-3">
          <svg width="36" height="36" viewBox="0 0 36 36" aria-hidden>
            <rect width="36" height="36" rx="9" fill="#2563eb" />
            <path d="M18 7l9 3.4v6.8c0 5.6-3.8 9.6-9 12.3-5.2-2.7-9-6.7-9-12.3v-6.8z" fill="#0b1b2e" />
            <path d="M13.5 18.2l3.2 3.2 6-6.4" fill="none" stroke="#6ee7b7" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <div>
            <h1 className="text-xl font-bold leading-6 tracking-tight">CashGuard</h1>
            <p className="text-sm text-slate-300">Ask before you pay: cash-safety decisions over API</p>
          </div>
        </div>
        <a href="#/" className="rounded-md border border-white/20 px-3 py-1.5 text-sm text-slate-200 hover:bg-white/10">&larr; CashGuard home</a>
        <div role="group" aria-label="Demo" className="flex overflow-hidden rounded-md border border-white/20 text-sm">
          {([['bills', 'Bill pay demo'], ['purchases', 'Purchase checks demo']] as const).map(([r, label]) => (
            <a key={r} href={`#/${r}`} aria-current={route === r ? 'page' : undefined}
              className={`px-3 py-1.5 ${route === r ? 'bg-white font-semibold text-cg-ink' : 'text-slate-200 hover:bg-white/10'}`}>{label}</a>
          ))}
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-x-4 gap-y-2">
          <ModeBadge conn={conn} />
          <div role="group" aria-label="Data source" className="flex overflow-hidden rounded-md border border-white/20 text-xs">
            {(['fixture', 'nessie'] as const).map(src => (
              <button key={src} onClick={() => void switchSource(src)} disabled={switching !== null}
                aria-pressed={conn?.mode === src}
                className={`px-2.5 py-1 disabled:opacity-60 ${conn?.mode === src ? 'bg-white/15 font-semibold text-white' : 'text-slate-300 hover:bg-white/10'}`}>
                {switching === src ? 'Syncing...' : src === 'fixture' ? 'Fixture' : 'Nessie'}
              </button>
            ))}
          </div>
          <button onClick={resetDemo} className="rounded-md border border-white/20 px-2.5 py-1 text-xs text-slate-200 hover:bg-white/10">
            Reset demo
          </button>
        </div>
      </header>
      {route === 'bills' && <DemoScript resetKey={resetKey} />}
      {notice && <div className="mb-3 rounded-lg border border-warn-line bg-warn-soft p-2 text-sm text-warn-ink">{notice}</div>}
      {error && <div className="mb-3 rounded-lg border border-block-line bg-block-soft p-2 text-sm text-block-ink">API error: {error}. Is the backend running on :8000?</div>}
      {route === 'purchases' ? <PurchasesDemo rev={rev} resetKey={resetKey} /> : (
        <>
      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
        <MockClient state={state} refresh={refresh} />
        <Inspector />
      </div>
      <div className="card mt-4">
        <div role="tablist" className="flex gap-1 border-b border-slate-200 px-3 pt-2">
          {TABS.map(t => (
            <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}
              className={`-mb-px border-b-2 px-4 py-2 text-sm font-medium ${tab === t ? 'border-cg-accent text-cg-accent' : 'border-transparent text-slate-500 hover:text-slate-800'}`}>
              {t}
            </button>
          ))}
        </div>
        <div className="p-4">
          {tab === 'TwinState' && <TwinStateTab state={state} />}
          {tab === 'Forecast' && <ForecastTab rev={rev} />}
          {tab === 'RiskLab' && <RiskLabTab state={state} rev={rev} />}
          {tab === 'Events' && <EventsTab rev={rev} />}
        </div>
      </div>
        </>
      )}
    </div>
  )
}
