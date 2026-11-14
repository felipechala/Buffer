import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { setRecorder, type CallRecord } from './api'

interface Ctx { calls: CallRecord[]; clear: () => void }
const InspectorCtx = createContext<Ctx>({ calls: [], clear: () => {} })
let nextId = 1

export function InspectorProvider({ children }: { children: ReactNode }) {
  const [calls, setCalls] = useState<CallRecord[]>([])
  useEffect(() => {
    setRecorder(r => setCalls(c => [{ ...r, id: nextId++ }, ...c].slice(0, 100)))
    return () => setRecorder(() => {})
  }, [])
  const clear = useCallback(() => setCalls([]), [])
  const v = useMemo(() => ({ calls, clear }), [calls, clear])
  return <InspectorCtx.Provider value={v}>{children}</InspectorCtx.Provider>
}
export const useInspector = () => useContext(InspectorCtx)
