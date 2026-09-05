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
  selectRun: (id: string) => Promise<void>
  runRecon: () => Promise<void>
  kickoffId: string | null
  bankPdfPath: string | null
  bankPdfMeta: { filename: string; records?: number; note?: string } | null
  setBankPdf: (path: string | null, meta?: { filename: string; records?: number; note?: string } | null) => void
  uploadBankPdf: (file: File) => Promise<void>
  resolve: (id: string, decision: string) => Promise<{
    workspace?: Record<string, unknown>
    human_decision?: string
    run_id?: string
  }>
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
  const [kickoffId, setKickoffId] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [useAi, setUseAi] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [bankPdfPath, setBankPdfPath] = useState<string | null>(null)
  const [bankPdfMeta, setBankPdfMeta] = useState<{
    filename: string
    records?: number
    note?: string
  } | null>(null)

  const setBankPdf = useCallback(
    (path: string | null, meta?: { filename: string; records?: number; note?: string } | null) => {
      setBankPdfPath(path)
      setBankPdfMeta(meta ?? null)
    },
    [],
  )

  const uploadBankPdf = useCallback(async (file: File) => {
    const res = await api.uploadBankPdf(file)
    setBankPdfPath(res.bank_pdf_path)
    const isUrl = /^https?:\/\//i.test(res.bank_pdf_path)
    setBankPdfMeta({
      filename: res.filename,
      records: res.preview?.records,
      note: res.public_error
        ? `Public URL publish failed: ${res.public_error}`
        : isUrl
          ? 'Public URL ready for CrewAI AMP fetch'
          : res.preview?.local_text_extract === 'ok'
            ? `Local text extract: ${res.preview.records} credits`
            : res.preview?.note || res.ocr,
    })
  }, [])

  const applyPayload = useCallback((payload: Awaited<ReturnType<typeof api.run>>) => {
    setRunId(payload.run.id)
    setStages(payload.run.stages || [])
    setMetrics(payload.metrics)
    setCash(
      (payload as { cash_position?: CashPosition }).cash_position ||
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
    () => transactions.filter((t) => t.kind === 'payment' && String(t.source || '') !== 'amp'),
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

  const selectRun = useCallback(
    async (id: string) => {
      try {
        const payload = await api.run(id)
        applyPayload(payload)
        setError(null)
      } catch (e) {
        setError(String(e))
      }
    },
    [applyPayload],
  )

  useEffect(() => {
    api.sources().then((r) => setSources(r.sources)).catch((e) => setError(String(e)))
    loadLatest()
  }, [loadLatest])

  const runRecon = useCallback(async () => {
    setRunning(true)
    setError(null)
    setKickoffId(null)
    try {
      const live = await api.reconcile({ useAi, bank_pdf_path: bankPdfPath })
      setRunId(live.run_id)
      setStages(live.stages || [])
      setMetrics({ ...live.metrics, details: live.details })
      setCash(live.cash_position || live.details?.cash_position || null)

      // AMP: kickoff returns immediately — poll until completed/failed
      if (live.engine === 'crewai-amp' && live.kickoff_id) {
        const id = live.kickoff_id
        setKickoffId(id)
        setStages([
          { name: 'crewai_amp_kickoff', status: 'completed' },
          { name: 'crewai_amp_wait', status: 'running', detail: { kickoff_id: id } },
        ])
        const deadline = Date.now() + 10 * 60 * 1000
        while (Date.now() < deadline) {
          await new Promise((r) => setTimeout(r, 3000))
          try {
            const st = await api.ampStatus(id)
            const state = String(st.state || '').toUpperCase()
            const statusText = String(st.status || '').toLowerCase()
            if (
              ['COMPLETED', 'SUCCESS', 'SUCCEEDED', 'DONE'].includes(state) ||
              statusText.includes('completed') ||
              statusText.includes('success')
            ) {
              // Single source of truth: promote AMP result into local dashboard state
              const finalized = await api.ampFinalize(id, { run_id: live.run_id })
              applyPayload(finalized)
              setStages(
                finalized.run?.stages || [
                  { name: 'crewai_amp_kickoff', status: 'completed' },
                  { name: 'crewai_amp_wait', status: 'completed' },
                ],
              )
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
        return
      }

      const payload = await api.run(live.run_id)
      applyPayload(payload)
    } catch (e) {
      setError(String(e))
    } finally {
      setRunning(false)
      setKickoffId(null)
    }
  }, [applyPayload, useAi, bankPdfPath])

  const resolve = useCallback(
    async (id: string, decision: string) => {
      try {
        const res = await api.resolve(id, decision)
        if (runId) applyPayload(await api.run(runId))
        return res
      } catch (e) {
        setError(String(e))
        throw e
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
      selectRun,
      runRecon,
      kickoffId,
      bankPdfPath,
      bankPdfMeta,
      setBankPdf,
      uploadBankPdf,
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
      kickoffId,
      running,
      useAi,
      error,
      loadLatest,
      selectRun,
      runRecon,
      bankPdfPath,
      bankPdfMeta,
      setBankPdf,
      uploadBankPdf,
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
