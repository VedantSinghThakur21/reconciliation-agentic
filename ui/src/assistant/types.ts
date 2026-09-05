import type { ExceptionItem, Metrics, ResultRow } from '../api'

/** Same auto-match band as finance_controller / pipeline auto_approve_confidence. */
export const HIGH_CONFIDENCE = 0.9

export const HINT =
  "Try: 'what's stuck on my plate?', 'show exceptions', 'strong matches', 'reconcile', 'dig into BNK-2101', 'greenlight BNK-2101', or 'reject BNK-2101'"

export type Intent =
  | 'attention'
  | 'exceptions'
  | 'high_confidence'
  | 'reconcile'
  | 'investigate'
  | 'approve'
  | 'reject'
  | 'unknown'

export type ResponseType =
  | 'exception_summary'
  | 'exception_table'
  | 'high_confidence_table'
  | 'reconcile_summary'
  | 'investigation'
  | 'confirmation'
  | 'hint'

export type AssistantEnvelope<T = unknown> = {
  intent: Intent
  response_type: ResponseType
  data: T
}

export type ExceptionSummaryData = {
  run_id: string | null
  pending_count: number
  total_exceptions: number
  categories: Array<{ reason: string; count: number }>
  top_priority: Array<{
    id: string
    payment_id: string
    reason?: string
    amount_applied: number
  }>
}

export type ExceptionTableData = {
  run_id: string | null
  rows: ExceptionItem[]
}

export type HighConfidenceData = {
  threshold: number
  rows: Array<{
    id: string
    payment_id?: string
    invoice_id?: string
    amount_applied: number
    confidence: number
  }>
  reconciled_total: number
}

export type ReconcileSummaryData = {
  run_id: string | null
  metrics: Metrics | null
  pending_exceptions: number
}

export type InvestigationData =
  | {
      found: true
      txn_ref: string
      amount_score: number
      merchant_score: number
      date_score: number
      reference_score: number
      confidence: number
      strategy: string
      matched_erp_txn_id: string
      matched_counter_txn_id: string
      candidate_id: string
      exception_id: string | null
      exception_reason: string | null
    }
  | {
      found: false
      txn_ref: string
      message: string
      exception_id: string | null
      exception_reason: string | null
    }

export type ConfirmationData = {
  ok: boolean
  txn_ref: string
  decision: 'confirmed' | 'rejected'
  exception_id: string | null
  status: string | null
  message: string
}

export type HintData = { message: string }

export type HandlerContext = {
  runId: string | null
  exceptions: ExceptionItem[]
  results: ResultRow[]
  metrics: Metrics | null
}

export type MatchCandidate = {
  id: string
  erp_txn_id: string
  counter_txn_id: string
  strategy?: string
  amount_score?: number
  merchant_score?: number
  date_score?: number
  reference_score?: number
  confidence?: number
}
