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
  reconcile: (opts?: { bank_pdf_path?: string | null; useAi?: boolean }) =>
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
      inputs_sent?: Record<string, unknown>
    }>('/api/reconcile', {
      method: 'POST',
      body: JSON.stringify({
        user_request: opts?.bank_pdf_path
          ? /^https?:\/\//i.test(opts.bank_pdf_path)
            ? `Run AR reconciliation. Bank PDF is at ${opts.bank_pdf_path}. Use payment_processor.csv and ground_truth.csv.`
            : 'Run AR reconciliation including uploaded bank statement PDF (AMP OCR if available)'
          : 'Run AR reconciliation on demo feeds',
        ar_ap_mode: 'AR',
        wait: false,
        bank_pdf_path: opts?.bank_pdf_path || undefined,
      }),
    }),
  uploadBankPdf: async (file: File) => {
    const form = new FormData()
    form.append('file', file)
    const res = await fetch(`${BASE}/api/uploads/bank-pdf`, { method: 'POST', body: form })
    if (!res.ok) throw new Error(await res.text())
    return res.json() as Promise<{
      filename: string
      path: string
      bank_pdf_path: string
      public_url?: string | null
      public_error?: string | null
      bytes: number
      preview: {
        local_text_extract: string | null
        records: number
        error?: string | null
        note?: string
      }
      ocr: string
    }>
  },
  listBankPdfs: () =>
    request<{ files: Array<{ filename: string; path: string; bytes: number }> }>('/api/uploads/bank-pdf'),
  ampStatus: (kickoffId: string) =>
    request<{
      state?: string
      status?: string
      result?: unknown
      result_json?: unknown
    }>(`/api/amp/status/${kickoffId}`),
  ampFinalize: (kickoffId: string, opts?: { run_id?: string }) =>
    request<RunPayload & { status?: string; metrics?: Metrics }>(`/api/amp/finalize/${kickoffId}`, {
      method: 'POST',
      body: JSON.stringify({ run_id: opts?.run_id }),
    }),
  journals: (runId?: string | null) =>
    request<{
      journals: Array<Record<string, unknown>>
      writebacks: Array<Record<string, unknown>>
      run_id?: string
    }>(runId ? `/api/journals?run_id=${encodeURIComponent(runId)}` : '/api/journals'),
  report: (runId?: string | null) =>
    request<{ run_id: string; report: string; evaluation?: Record<string, unknown> }>(
      runId ? `/api/report?run_id=${encodeURIComponent(runId)}` : '/api/report',
    ),
  candidates: (runId?: string | null) =>
    request<{ candidates: Array<Record<string, unknown>> }>(
      runId ? `/api/candidates?run_id=${encodeURIComponent(runId)}` : '/api/candidates',
    ),
  listRuns: (limit = 50) =>
    request<{
      runs: Array<{
        id: string
        user_request?: string
        ar_ap_mode?: string
        status: string
        started_at: string
        finished_at?: string | null
        bank_pdf_path?: string | null
        summary_preview?: {
          match_rate?: number | null
          accuracy?: number | null
          f1?: number | null
          exception_count?: number | null
          auto_match_count?: number | null
        }
      }>
      active_run_id: string | null
    }>(`/api/runs?limit=${limit}`),
  latest: () => request<RunPayload>('/api/runs/latest'),
  run: (id: string) => request<RunPayload>(`/api/runs/${id}`),
  resolve: (id: string, decision: string, note?: string) =>
    request<
      ExceptionItem & {
        human_decision?: string
        workspace?: Record<string, unknown>
        run_id?: string
      }
    >(`/api/exceptions/${id}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ decision, decided_by: 'analyst', note }),
    }),
  qa: (question: string, runId?: string) =>
    request<{ answer: string; run_id: string; question: string }>('/api/qa', {
      method: 'POST',
      body: JSON.stringify({ question, run_id: runId }),
    }),
  classify: (text: string) =>
    request<{
      intent: string
      txn_ref: string | null
      source?: string
      llm_intent?: string
      text: string
    }>('/api/assistant/classify', {
      method: 'POST',
      body: JSON.stringify({ text }),
    }),
  audit: (runId?: string) =>
    request<{ events: Array<Record<string, unknown>> }>(
      runId ? `/api/audit?run_id=${runId}` : '/api/audit',
    ),
}
