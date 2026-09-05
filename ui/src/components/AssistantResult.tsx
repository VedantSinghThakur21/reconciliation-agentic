import { Link } from 'react-router-dom'
import { money, pct } from '../lib/format'
import { HIGH_CONFIDENCE } from '../assistant/types'
import type {
  AssistantEnvelope,
  ConfirmationData,
  ExceptionSummaryData,
  ExceptionTableData,
  HighConfidenceData,
  HintData,
  InvestigationData,
  ReconcileSummaryData,
} from '../assistant/types'

function ScoreBar({ label, value }: { label: string; value: number }) {
  const pctVal = Math.round(Math.max(0, Math.min(1, value)) * 100)
  return (
    <div className="score-bar">
      <div className="score-bar-label">
        <span>{label}</span>
        <span className="mono">{pctVal}%</span>
      </div>
      <div className="score-bar-track">
        <div className="score-bar-fill" style={{ width: `${pctVal}%` }} />
      </div>
    </div>
  )
}

export function AssistantResult({ envelope }: { envelope: AssistantEnvelope }) {
  const { response_type, data } = envelope

  if (response_type === 'hint') {
    const d = data as HintData
    return (
      <div className="dash-query-card">
        <p className="dash-query-hint">{d.message}</p>
      </div>
    )
  }

  if (response_type === 'exception_summary') {
    const d = data as ExceptionSummaryData
    return (
      <div className="dash-query-card">
        <h3>Exception summary</h3>
        {!d.run_id ? (
          <p className="card-sub">No run loaded — start reconciliation first.</p>
        ) : (
          <>
            <div className="grid-3" style={{ marginBottom: 12 }}>
              <div className="stat">
                <div className="label">Open exceptions</div>
                <div className="value">{d.pending_count}</div>
              </div>
              <div className="stat">
                <div className="label">Categories</div>
                <div className="value">{d.categories.length}</div>
              </div>
              <div className="stat">
                <div className="label">Total exceptions</div>
                <div className="value">{d.total_exceptions}</div>
              </div>
            </div>
            <p className="card-sub" style={{ marginBottom: 8 }}>
              Breakdown by category
            </p>
            <ul className="dash-query-list">
              {d.categories.length === 0 && <li>None pending</li>}
              {d.categories.map((c) => (
                <li key={c.reason}>
                  <span className="mono">{c.reason}</span>
                  <strong>{c.count}</strong>
                </li>
              ))}
            </ul>
            <p className="card-sub" style={{ margin: '12px 0 8px' }}>
              Top 3 by amount applied
            </p>
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Payment</th>
                    <th>Reason</th>
                    <th>Amount</th>
                  </tr>
                </thead>
                <tbody>
                  {d.top_priority.length === 0 ? (
                    <tr>
                      <td colSpan={3}>No pending items</td>
                    </tr>
                  ) : (
                    d.top_priority.map((ex) => (
                      <tr key={ex.id}>
                        <td className="mono">{ex.payment_id}</td>
                        <td>{ex.reason || '—'}</td>
                        <td>{money(ex.amount_applied)}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
            <p className="card-sub" style={{ marginTop: 10 }}>
              Same source as <Link to="/reviews">Reviews</Link> ({d.pending_count} pending).
            </p>
          </>
        )}
      </div>
    )
  }

  if (response_type === 'exception_table') {
    const d = data as ExceptionTableData
    return (
      <div className="dash-query-card">
        <h3>HITL review queue</h3>
        {!d.run_id || d.rows.length === 0 ? (
          <p className="card-sub">No exceptions for this run.</p>
        ) : (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Payment</th>
                  <th>Invoice</th>
                  <th>Status</th>
                  <th>Reason</th>
                  <th>Conf.</th>
                  <th>Amount</th>
                </tr>
              </thead>
              <tbody>
                {d.rows.map((ex) => (
                  <tr key={ex.id}>
                    <td className="mono">{ex.payment_id}</td>
                    <td className="mono">{ex.invoice_id || '—'}</td>
                    <td>
                      <span className={`badge ${ex.status === 'pending' ? 'badge-warn' : 'badge-muted'}`}>
                        {ex.status}
                      </span>
                    </td>
                    <td>{ex.reason || '—'}</td>
                    <td>{(Number(ex.confidence) * 100).toFixed(0)}%</td>
                    <td>{money(ex.amount_applied)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="card-sub" style={{ marginTop: 10 }}>
          Full queue with approve/reject: <Link to="/reviews">Reviews</Link>
        </p>
      </div>
    )
  }

  if (response_type === 'high_confidence_table') {
    const d = data as HighConfidenceData
    return (
      <div className="dash-query-card">
        <h3>High confidence matches (≥{(HIGH_CONFIDENCE * 100).toFixed(0)}%)</h3>
        <p className="card-sub">
          Reconciled results at or above the existing auto-match band ({d.threshold}). Showing{' '}
          {d.rows.length} of {d.reconciled_total} matched.
        </p>
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Transaction ref</th>
                <th>Invoice</th>
                <th>Amount</th>
                <th>Confidence</th>
              </tr>
            </thead>
            <tbody>
              {d.rows.length === 0 ? (
                <tr>
                  <td colSpan={4}>No high-confidence matches in this run</td>
                </tr>
              ) : (
                d.rows.map((r) => (
                  <tr key={r.id}>
                    <td className="mono">{r.payment_id || r.id}</td>
                    <td className="mono">{r.invoice_id || '—'}</td>
                    <td>{money(r.amount_applied)}</td>
                    <td>{(Number(r.confidence) * 100).toFixed(0)}%</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
        <p className="card-sub" style={{ marginTop: 10 }}>
          Source: Matches page reconciled rows · <Link to="/matches">open Matches</Link>
        </p>
      </div>
    )
  }

  if (response_type === 'reconcile_summary') {
    const d = data as ReconcileSummaryData
    return (
      <div className="dash-query-card">
        <h3>Reconciliation summary</h3>
        {!d.metrics ? (
          <p className="card-sub">Run finished but metrics are not available yet.</p>
        ) : (
          <div className="grid-5">
            <div className="stat">
              <div className="label">Total processed</div>
              <div className="value">{d.metrics.total_payments ?? '—'}</div>
            </div>
            <div className="stat">
              <div className="label">Matched</div>
              <div className="value">{d.metrics.auto_reconciled ?? '—'}</div>
            </div>
            <div className="stat">
              <div className="label">Review</div>
              <div className="value">{d.metrics.pending_review ?? '—'}</div>
            </div>
            <div className="stat">
              <div className="label">Exceptions</div>
              <div className="value">{d.pending_exceptions}</div>
            </div>
            <div className="stat">
              <div className="label">Match rate</div>
              <div className="value">{pct(d.metrics.match_rate)}</div>
            </div>
          </div>
        )}
        <p className="card-sub" style={{ marginTop: 10 }}>
          Same stats as the dashboard scorecard · run {d.run_id || '—'}
        </p>
      </div>
    )
  }

  if (response_type === 'investigation') {
    const d = data as InvestigationData
    if (!d.found) {
      return (
        <div className="dash-query-card">
          <h3>Investigation · {d.txn_ref}</h3>
          <p className="dash-query-hint">{d.message}</p>
          {d.exception_id && (
            <p className="card-sub" style={{ marginTop: 8 }}>
              Exception {d.exception_id}
              {d.exception_reason ? ` · ${d.exception_reason}` : ''} (no candidate score join)
            </p>
          )}
        </div>
      )
    }
    return (
      <div className="dash-query-card">
        <h3>Investigation · {d.txn_ref}</h3>
        <p className="card-sub">
          Candidate {d.candidate_id} · strategy {d.strategy} ·{' '}
          <span className="mono">{d.matched_counter_txn_id}</span> ↔{' '}
          <span className="mono">{d.matched_erp_txn_id}</span>
        </p>
        <div className="stat" style={{ marginBottom: 12 }}>
          <div className="label">Overall confidence</div>
          <div className="value">{(d.confidence * 100).toFixed(0)}%</div>
        </div>
        <ScoreBar label="Amount match" value={d.amount_score} />
        <ScoreBar label="Merchant / name match" value={d.merchant_score} />
        <ScoreBar label="Date match" value={d.date_score} />
        <ScoreBar label="Reference match" value={d.reference_score} />
        {d.exception_id && (
          <p className="card-sub" style={{ marginTop: 12 }}>
            Linked exception {d.exception_id}
            {d.exception_reason ? ` · ${d.exception_reason}` : ''}
          </p>
        )}
      </div>
    )
  }

  if (response_type === 'confirmation') {
    const d = data as ConfirmationData
    return (
      <div className="dash-query-card">
        <h3>{d.decision === 'confirmed' ? 'Approved' : 'Rejected'} · {d.txn_ref}</h3>
        {d.ok ? (
          <>
            <p className="card-sub">{d.message}</p>
            <div className="grid-3" style={{ marginTop: 12 }}>
              <div className="stat">
                <div className="label">Transaction</div>
                <div className="value" style={{ fontSize: 18 }}>
                  <span className="mono">{d.txn_ref}</span>
                </div>
              </div>
              <div className="stat">
                <div className="label">Exception</div>
                <div className="value" style={{ fontSize: 16 }}>
                  <span className="mono">{d.exception_id || '—'}</span>
                </div>
              </div>
              <div className="stat">
                <div className="label">Decision</div>
                <div className="value" style={{ fontSize: 18 }}>
                  <span className={`badge ${d.decision === 'confirmed' ? 'badge-ok' : 'badge-warn'}`}>
                    {d.status || d.decision}
                  </span>
                </div>
              </div>
            </div>
            <p className="card-sub" style={{ marginTop: 10 }}>
              Wrote review via existing resolve endpoint (no ledger mutation).
            </p>
          </>
        ) : (
          <p className="dash-query-hint">{d.message}</p>
        )}
      </div>
    )
  }

  return null
}
