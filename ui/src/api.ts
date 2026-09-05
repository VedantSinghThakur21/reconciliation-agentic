const BASE = ''

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    ...init,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || res.statusText)
  }
  return res.json() as Promise<T>
}

export type Source = {
  id: string
  label: string
  mode: string
  count: number
  kind: string
  status: string
  live: boolean
}

export type Stage = {
  name: string
  status: string
  detail?: Record<string, unknown>
}

export type CashPosition = {
  cash_applied?: number
  open_ar?: number
  unapplied_cash?: number
  net_exposure?: number
  invoice_count?: number
  payment_count?: number
}

export type Metrics = {
  accuracy?: number | null
  precision?: number | null
  recall?: number | null
  f1?: number | null
  match_rate?: number | null
  auto_reconciled?: number
  pending_review?: number
  total_payments?: number
  unreconciled_amount?: number
  details?: {
    throughput_per_sec?: number
    duration_sec?: number
    ai_enabled?: boolean
    ai_suggestions?: number
    cash_position?: CashPosition
    confusion?: Record<string, number>
  }
}

export type ExceptionItem = {
  id: string
  run_id: string
  payment_id: string
  invoice_id?: string | null
  confidence: number
  reason?: string
  reasoning?: string
  amount_applied: number
  status: string
  created_at: string
  decided_at?: string | null
  decided_by?: string | null
}

export type ResultRow = {
  id: string
  payment_id?: string
  invoice_id?: string
  status: string
  confidence: number
  tier?: string
  amount_applied: number
  treatment?: string
  reasoning?: string
  source?: string
}

export type TransactionRow = {
  id: string
  run_id: string
  source: string
  kind: string
  external_id: string
  party_name?: string | null
  amount: number
  currency: string
  txn_date?: string | null
  reference?: string | null
  raw?: Record<string, unknown>
}

export type RunPayload = {
  run: {
    id: string
    status: string
    started_at: string
    finished_at?: string
    stages: Stage[]
    summary: Record<string, unknown>
    mode: string
    use_ai?: boolean
  }
  metrics: Metrics | null
  exceptions: ExceptionItem[]
  results: ResultRow[]
  transactions: TransactionRow[]
  audit: Array<Record<string, unknown>>
}

export const api = {
  health: () => request<{ status: string }>('/api/health'),
  sources: () => request<{ sources: Source[] }>('/api/sources'),
  reconcile: (_useAi = true) =>
    request<{
      run_id: string
      summary: Record<string, unknown>
      stages: Stage[]
      metrics: Metrics
      cash_position?: CashPosition
      details?: Metrics['details']
      confirmation_message?: string
      final_report?: string
      journals?: Record<string, unknown>
      kickoff_id?: string
      engine?: string
      amp_result?: Record<string, unknown>
    }>('/api/reconcile', {
      method: 'POST',
      body: JSON.stringify({
        user_request: 'Run AR reconciliation on demo feeds',
        ar_ap_mode: 'AR',
        wait: false,
      }),
    }),
  ampStatus: (kickoffId: string) =>
    request<{
      state?: string
      status?: string
      result?: unknown
      result_json?: unknown
    }>(`/api/amp/status/${kickoffId}`),
  journals: () =>
    request<{ journals: Array<Record<string, unknown>>; writebacks: Array<Record<string, unknown>> }>(
      '/api/journals',
    ),
  report: () => request<{ run_id: string; report: string }>('/api/report'),
  candidates: () => request<{ candidates: Array<Record<string, unknown>> }>('/api/candidates'),
  latest: () => request<RunPayload>('/api/runs/latest'),
  run: (id: string) => request<RunPayload>(`/api/runs/${id}`),
  resolve: (id: string, decision: string, note?: string) =>
    request<ExceptionItem>(`/api/exceptions/${id}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ decision, decided_by: 'analyst', note }),
    }),
  qa: (question: string, runId?: string) =>
    request<{ answer: string; run_id: string; question: string }>('/api/qa', {
      method: 'POST',
      body: JSON.stringify({ question, run_id: runId }),
    }),
  audit: (runId?: string) =>
    request<{ events: Array<Record<string, unknown>> }>(
      runId ? `/api/audit?run_id=${runId}` : '/api/audit',
    ),
}
