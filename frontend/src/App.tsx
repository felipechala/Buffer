import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from './api'
import type { ConnectionsResponse, TwinStateResponse } from './types'
import MockClient from './components/MockClient'
import Inspector from './components/Inspector'
import ModeBadge from './components/ModeBadge'
import TwinStateTab from './tabs/TwinState'
import ForecastTab from './tabs/Forecast'
import RiskLabTab from './tabs/RiskLab'
import EventsTab from './tabs/Events'

const TABS = ['TwinState', 'Forecast', 'RiskLab', 'Events'] as const
type Tab = typeof TABS[number]

export default function App() {
  const [conn, setConn] = useState<ConnectionsResponse | null>(null)
  const [state, setState] = useState<TwinStateResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('TwinState')
  const [rev, setRev] = useState(0) // bumps whenever the twin snapshot may have changed

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
    try { await api.syncFixture(true); await refresh() } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }

  return (
    <div className="mx-auto max-w-[1500px] p-4">
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-bold text-slate-800">CashGuard <span className="font-normal text-slate-500">· pre-payment cash-safety API demo</span></h1>
        <ModeBadge conn={conn} />
        <div role="group" aria-label="Data source" className="flex overflow-hidden rounded border border-slate-300 text-xs">
          {(['fixture', 'nessie'] as const).map(src => (
            <button key={src} onClick={() => void switchSource(src)} disabled={switching !== null}
              aria-pressed={conn?.mode === src}
              className={`px-3 py-1 disabled:opacity-60 ${conn?.mode === src ? 'bg-violet-700 font-semibold text-white' : 'bg-white hover:bg-slate-50'}`}>
              {switching === src ? 'Syncing...' : src === 'fixture' ? 'Fixture' : 'Nessie'}
            </button>
          ))}
        </div>
        <button onClick={resetDemo} className="ml-auto rounded border border-slate-300 bg-white px-3 py-1 text-xs hover:bg-slate-50">
          Reset demo
        </button>
      </div>
      {notice && <div className="mb-3 rounded bg-amber-50 p-2 text-sm text-amber-800">{notice}</div>}
      {error && <div className="mb-3 rounded bg-red-50 p-2 text-sm text-red-700">API error: {error}. Is the backend running on :8000?</div>}
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
        <MockClient state={state} refresh={refresh} />
        <Inspector />
      </div>
      <div className="mt-4 rounded-xl border border-slate-200 bg-white shadow-sm">
        <div role="tablist" className="flex gap-1 border-b border-slate-200 px-3 pt-2">
          {TABS.map(t => (
            <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}
              className={`rounded-t px-4 py-2 text-sm font-medium ${tab === t ? 'border-b-2 border-violet-600 text-violet-700' : 'text-slate-500 hover:text-slate-800'}`}>
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
    </div>
  )
}
