import { useCallback, useEffect, useState } from 'react'
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

  useEffect(() => { void refresh() }, [refresh])

  async function resetDemo() {
    try { await api.syncFixture(true); await refresh() } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }

  return (
    <div className="mx-auto max-w-[1500px] p-4">
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-bold text-slate-800">CashGuard <span className="font-normal text-slate-500">· pre-payment cash-safety API demo</span></h1>
        <ModeBadge conn={conn} />
        <button onClick={resetDemo} className="ml-auto rounded border border-slate-300 bg-white px-3 py-1 text-xs hover:bg-slate-50">
          Reset demo (fixture sync)
        </button>
      </div>
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
