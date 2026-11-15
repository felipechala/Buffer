import { lazy, Suspense, useEffect, useState } from 'react'

const Landing = lazy(() => import('./site/Landing'))
// Static GitHub Pages build (no backend): demos are compiled out, only the landing page ships.
const STATIC_SITE = import.meta.env.VITE_STATIC_SITE === '1'
const DemoApp = STATIC_SITE ? null : lazy(() => import('./DemoApp'))

type Route = 'home' | 'bills' | 'purchases'
const readRoute = (): Route => {
  if (STATIC_SITE) return 'home'
  const h = window.location.hash
  return h.startsWith('#/purchases') ? 'purchases' : h.startsWith('#/bills') ? 'bills' : 'home'
}

function Loading() {
  return (
    <div role="status" aria-live="polite" className="flex min-h-screen items-center justify-center bg-slate-50 text-sm text-slate-500">
      <span className="mr-3 inline-block h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-brand" aria-hidden />
      Loading Buffer...
    </div>
  )
}

export default function App() {
  const [route, setRoute] = useState<Route>(readRoute)
  useEffect(() => {
    const on = () => setRoute(readRoute())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  useEffect(() => {
    document.title = route === 'home' ? 'Buffer — cash-safety checks before money moves' : 'Buffer demo'
    if (route !== 'home') window.scrollTo(0, 0)
  }, [route])
  return (
    <Suspense fallback={<Loading />}>
      {route === 'home' || !DemoApp ? <Landing /> : <DemoApp route={route} />}
    </Suspense>
  )
}
