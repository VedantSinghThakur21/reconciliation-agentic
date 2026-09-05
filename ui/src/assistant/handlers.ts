import { api } from '../api'
import {
  HIGH_CONFIDENCE,
  HINT,
  type AssistantEnvelope,
  type ConfirmationData,
  type ExceptionSummaryData,
  type ExceptionTableData,
  type HandlerContext,
  type HighConfidenceData,
  type HintData,
  type InvestigationData,
  type MatchCandidate,
  type ReconcileSummaryData,
} from './types'

export function buildAttention(ctx: HandlerContext): AssistantEnvelope<ExceptionSummaryData> {
  const pending = ctx.exceptions.filter((e) => e.status === 'pending')
  const map = new Map<string, number>()
  for (const e of pending) {
    const key = (e.reason || 'unclassified').trim() || 'unclassified'
    map.set(key, (map.get(key) || 0) + 1)
  }
  const categories = [...map.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([reason, count]) => ({ reason, count }))
  const top_priority = [...pending]
    .sort((a, b) => Number(b.amount_applied || 0) - Number(a.amount_applied || 0))
    .slice(0, 3)
    .map((ex) => ({
      id: ex.id,
      payment_id: ex.payment_id,
      reason: ex.reason,
      amount_applied: Number(ex.amount_applied || 0),
    }))

  return {
    intent: 'attention',
    response_type: 'exception_summary',
    data: {
      run_id: ctx.runId,
      pending_count: pending.length,
      total_exceptions: ctx.exceptions.length,
      categories,
      top_priority,
    },
  }
}

export function buildExceptions(ctx: HandlerContext): AssistantEnvelope<ExceptionTableData> {
  return {
    intent: 'exceptions',
    response_type: 'exception_table',
    data: {
      run_id: ctx.runId,
      rows: ctx.exceptions,
    },
  }
}

export function buildHighConfidence(ctx: HandlerContext): AssistantEnvelope<HighConfidenceData> {
  const reconciled = ctx.results.filter((r) => r.status === 'reconciled')
  const rows = reconciled
    .filter((r) => Number(r.confidence) >= HIGH_CONFIDENCE)
    .sort((a, b) => Number(b.confidence) - Number(a.confidence))
    .map((r) => ({
      id: r.id,
      payment_id: r.payment_id,
      invoice_id: r.invoice_id,
      amount_applied: Number(r.amount_applied || 0),
      confidence: Number(r.confidence || 0),
    }))

  return {
    intent: 'high_confidence',
    response_type: 'high_confidence_table',
    data: {
      threshold: HIGH_CONFIDENCE,
      rows,
      reconciled_total: reconciled.length,
    },
  }
}

export function buildReconcileSummary(ctx: HandlerContext): AssistantEnvelope<ReconcileSummaryData> {
  return {
    intent: 'reconcile',
    response_type: 'reconcile_summary',
    data: {
      run_id: ctx.runId,
      metrics: ctx.metrics,
      pending_exceptions: ctx.exceptions.filter((e) => e.status === 'pending').length,
    },
  }
}

export function buildHint(): AssistantEnvelope<HintData> {
  return {
    intent: 'unknown',
    response_type: 'hint',
    data: { message: HINT },
  }
}

function asCandidate(row: Record<string, unknown>): MatchCandidate {
  return {
    id: String(row.id || ''),
    erp_txn_id: String(row.erp_txn_id || ''),
    counter_txn_id: String(row.counter_txn_id || ''),
    strategy: row.strategy != null ? String(row.strategy) : undefined,
    amount_score: row.amount_score != null ? Number(row.amount_score) : undefined,
    merchant_score: row.merchant_score != null ? Number(row.merchant_score) : undefined,
    date_score: row.date_score != null ? Number(row.date_score) : undefined,
    reference_score: row.reference_score != null ? Number(row.reference_score) : undefined,
    confidence: row.confidence != null ? Number(row.confidence) : undefined,
  }
}

/** Read-only join: txn ref → exception + match_candidates scores. */
export async function buildInvestigation(
  txnRef: string,
  ctx: HandlerContext,
): Promise<AssistantEnvelope<InvestigationData>> {
  const ref = txnRef.trim().toUpperCase()
  const exception =
    ctx.exceptions.find((e) => String(e.payment_id || '').toUpperCase() === ref) || null

  let candidates: MatchCandidate[] = []
  try {
    const payload = await api.candidates()
    candidates = (payload.candidates || []).map((c) => asCandidate(c as Record<string, unknown>))
  } catch {
    candidates = []
  }

  const match =
    candidates.find(
      (c) =>
        c.counter_txn_id.toUpperCase() === ref || c.erp_txn_id.toUpperCase() === ref,
    ) || null

  if (!match) {
    return {
      intent: 'investigate',
      response_type: 'investigation',
      data: {
        found: false,
        txn_ref: ref,
        message: 'No detailed scores available for this transaction',
        exception_id: exception?.id ?? null,
        exception_reason: exception?.reason ?? null,
      },
    }
  }

  return {
    intent: 'investigate',
    response_type: 'investigation',
    data: {
      found: true,
      txn_ref: ref,
      amount_score: Number(match.amount_score ?? 0),
      merchant_score: Number(match.merchant_score ?? 0),
      date_score: Number(match.date_score ?? 0),
      reference_score: Number(match.reference_score ?? 0),
      confidence: Number(match.confidence ?? 0),
      strategy: String(match.strategy || '—'),
      matched_erp_txn_id: match.erp_txn_id,
      matched_counter_txn_id: match.counter_txn_id,
      candidate_id: match.id,
      exception_id: exception?.id ?? null,
      exception_reason: exception?.reason ?? null,
    },
  }
}

/** Lookup exception by txn ref → existing POST /api/exceptions/{id}/resolve. */
export async function buildConfirmation(
  txnRef: string,
  decision: 'confirmed' | 'rejected',
  ctx: HandlerContext,
): Promise<AssistantEnvelope<ConfirmationData>> {
  const ref = txnRef.trim().toUpperCase()
  const exception =
    ctx.exceptions.find((e) => String(e.payment_id || '').toUpperCase() === ref) || null

  if (!exception) {
    return {
      intent: decision === 'confirmed' ? 'approve' : 'reject',
      response_type: 'confirmation',
      data: {
        ok: false,
        txn_ref: ref,
        decision,
        exception_id: null,
        status: null,
        message: `No exception found for ${ref} — cannot submit ${decision}`,
      },
    }
  }

  try {
    const res = await api.resolve(exception.id, decision, `Assistant ${decision} for ${ref}`)
    return {
      intent: decision === 'confirmed' ? 'approve' : 'reject',
      response_type: 'confirmation',
      data: {
        ok: true,
        txn_ref: ref,
        decision,
        exception_id: exception.id,
        status: res.status || decision,
        message: `Submitted ${decision} for ${ref} via ${exception.id}`,
      },
    }
  } catch (e) {
    return {
      intent: decision === 'confirmed' ? 'approve' : 'reject',
      response_type: 'confirmation',
      data: {
        ok: false,
        txn_ref: ref,
        decision,
        exception_id: exception.id,
        status: null,
        message: `Resolve failed for ${exception.id}: ${String(e)}`,
      },
    }
  }
}
