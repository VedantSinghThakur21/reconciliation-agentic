import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import {
  api,
  type CashPosition,
  type ExceptionItem,
  type Metrics,
  type ResultRow,
  type Source,
  type Stage,
  type TransactionRow,
} from '../api'

type RunContextValue = {
  sources: Source[]
  stages: Stage[]
  metrics: Metrics | null
  cash: CashPosition | null
  results: ResultRow[]
  exceptions: ExceptionItem[]
  transactions: TransactionRow[]
  invoices: TransactionRow[]
  payments: TransactionRow[]
  audit: Array<Record<string, unknown>>
  runId: string | null
  running: boolean
  useAi: boolean
  setUseAi: (v: boolean) => void
  error: string | null
  setError: (v: string | null) => void
  loadLatest: () => Promise<void>
  runRecon: () => Promise<void>
  resolve: (id: string, decision: string) => Promise<void>
  ask: (question: string) => Promise<string>
  refreshAudit: () => Promise<void>
}

const RunContext = createContext<RunContextValue | null>(null)

export function RunProvider({ children }: { children: ReactNode }) {
  const [sources, setSources] = useState<Source[]>([])
  const [stages, setStages] = useState<Stage[]>([])
  const [metrics, setMetrics] = useState<Metrics | null>(null)
  const [cash, setCash] = useState<CashPosition | null>(null)
  const [results, setResults] = useState<ResultRow[]>([])
  const [exceptions, setExceptions] = useState<ExceptionItem[]>([])
  const [transactions, setTransactions] = useState<TransactionRow[]>([])
  const [audit, setAudit] = useState<Array<Record<string, unknown>>>([])
  const [runId, setRunId] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [useAi, setUseAi] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const applyPayload = useCallback((payload: Awaited<ReturnType<typeof api.run>>) => {
    setRunId(payload.run.id)
    setStages(payload.run.stages || [])
    setMetrics(payload.metrics)
    setCash(
      (payload.metrics?.details?.cash_position as CashPosition | undefined) ||
        ((payload.run.summary?.cash_position as CashPosition | undefined) ?? null),
    )
    setResults(payload.results || [])
    setExceptions(payload.exceptions || [])
    setTransactions(payload.transactions || [])
    setAudit(payload.audit || [])
  }, [])

  const invoices = useMemo(
    () => transactions.filter((t) => t.kind === 'invoice'),
    [transactions],
  )
  const payments = useMemo(
    () => transactions.filter((t) => t.kind === 'payment'),
    [transactions],
  )

  const loadLatest = useCallback(async () => {
    try {
      const payload = await api.latest()
      applyPayload(payload)
      setError(null)
    } catch {
      // no runs yet
    }
  }, [applyPayload])

  useEffect(() => {
    api.sources().then((r) => setSources(r.sources)).catch((e) => setError(String(e)))
    loadLatest()
  }, [loadLatest])

  const runRecon = useCallback(async () => {
    setRunning(true)
    setError(null)
    try {
      const live = await api.reconcile(useAi)
      setRunId(live.run_id)
      setStages(live.stages || [])
      setMetrics({ ...live.metrics, details: live.details })
      setCash(live.cash_position || live.details?.cash_position || null)

      // AMP: kickoff returns immediately — poll until completed/failed
      if (live.engine === 'crewai-amp' && live.kickoff_id) {
        const kickoffId = live.kickoff_id
        setStages([
          { name: 'crewai_amp_kickoff', status: 'completed' },
          { name: 'crewai_amp_wait', status: 'running', detail: { kickoff_id: kickoffId } },
        ])
        const deadline = Date.now() + 10 * 60 * 1000
        let finalReport: string | null =
          typeof live.final_report === 'string' ? live.final_report : null
        while (Date.now() < deadline) {
          await new Promise((r) => setTimeout(r, 3000))
          try {
            const st = await api.ampStatus(kickoffId)
            const state = String(st.state || '').toUpperCase()
            const statusText = String(st.status || '').toLowerCase()
            if (
              ['COMPLETED', 'SUCCESS', 'SUCCEEDED', 'DONE'].includes(state) ||
              statusText.includes('completed') ||
              statusText.includes('success')
            ) {
              finalReport =
                (typeof st.result === 'string' ? st.result : null) ||
                (st.result_json != null ? JSON.stringify(st.result_json, null, 2) : null) ||
                finalReport
              setStages([
                { name: 'crewai_amp_kickoff', status: 'completed' },
                { name: 'crewai_amp_wait', status: 'completed' },
              ])
              break
            }
            if (['FAILED', 'ERROR', 'CANCELLED', 'CANCELED'].includes(state)) {
              throw new Error(`AMP run failed: ${st.status || state}`)
            }
          } catch (pollErr) {
            // Keep polling on transient 404s right after kickoff
            const msg = String(pollErr)
            if (!msg.includes('404') && !msg.includes('Status not available')) {
              throw pollErr
            }
          }
        }
        if (finalReport) {
          // soft-load local payload if any; AMP report is primary
          try {
            applyPayload(await api.run(live.run_id))
          } catch {
            /* AMP-only run may have sparse local tables */
          }
        } else {
          try {
            applyPayload(await api.run(live.run_id))
          } catch {
            /* ignore */
          }
        }
        return
      }

      const payload = await api.run(live.run_id)
      applyPayload(payload)
    } catch (e) {
      setError(String(e))
    } finally {
      setRunning(false)
    }
  }, [applyPayload, useAi])

  const resolve = useCallback(
    async (id: string, decision: string) => {
      try {
        await api.resolve(id, decision)
        if (runId) applyPayload(await api.run(runId))
      } catch (e) {
        setError(String(e))
      }
    },
    [applyPayload, runId],
  )

  const refreshAudit = useCallback(async () => {
    if (!runId) return
    const a = await api.audit(runId)
    setAudit(a.events)
  }, [runId])

  const ask = useCallback(
    async (question: string) => {
      const res = await api.qa(question, runId || undefined)
      await refreshAudit()
      return res.answer
    },
    [refreshAudit, runId],
  )

  const value = useMemo(
    () => ({
      sources,
      stages,
      metrics,
      cash,
      results,
      exceptions,
      transactions,
      invoices,
      payments,
      audit,
      runId,
      running,
      useAi,
      setUseAi,
      error,
      setError,
      loadLatest,
      runRecon,
      resolve,
      ask,
      refreshAudit,
    }),
    [
      sources,
      stages,
      metrics,
      cash,
      results,
      exceptions,
      transactions,
      invoices,
      payments,
      audit,
      runId,
      running,
      useAi,
      error,
      loadLatest,
      runRecon,
      resolve,
      ask,
      refreshAudit,
    ],
  )

  return <RunContext.Provider value={value}>{children}</RunContext.Provider>
}

export function useRun() {
  const ctx = useContext(RunContext)
  if (!ctx) throw new Error('useRun must be used within RunProvider')
  return ctx
}
