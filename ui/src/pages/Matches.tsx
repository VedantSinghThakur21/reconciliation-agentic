import { useMemo, useState } from 'react'
import { money } from '../lib/format'
import { useRun } from '../state/RunContext'

export default function MatchesPage() {
  const { results, runId, metrics } = useRun()
  const [filter, setFilter] = useState<'all' | 'reconciled' | 'pending_review'>('all')
  const [q, setQ] = useState('')

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return results.filter((r) => {
      if (filter !== 'all' && r.status !== filter) return false
      if (!needle) return true
      return [r.payment_id, r.invoice_id, r.source, r.tier]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(needle))
    })
  }, [results, filter, q])

  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>Matches</h1>
          <p>
            Decisions produced by the reconciliation engine — rule matches and AI suggestions.
            {metrics ? ` ${metrics.auto_reconciled ?? 0} auto-reconciled · ${metrics.pending_review ?? 0} in review.` : ''}
          </p>
        </div>
      </div>

      <div className="card">
        <div
          className="card-pad"
          style={{
            borderBottom: '1px solid var(--line)',
            display: 'flex',
            justifyContent: 'space-between',
            gap: 12,
            alignItems: 'center',
            flexWrap: 'wrap',
          }}
        >
          <div className="row-actions">
            {([
              ['all', 'All'],
              ['reconciled', 'Matched'],
              ['pending_review', 'Needs review'],
            ] as const).map(([id, label]) => (
              <button
                key={id}
                className={`btn btn-sm ${filter === id ? 'btn-primary' : 'btn-secondary'}`}
                onClick={() => setFilter(id)}
              >
                {label}
              </button>
            ))}
          </div>
          <input className="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search matches…" />
        </div>

        {!runId || results.length === 0 ? (
          <div className="empty">
            <strong>No match decisions yet</strong>
            Run reconciliation to generate the payment → invoice ledger.
          </div>
        ) : (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Payment</th>
                  <th>Invoice</th>
                  <th>Status</th>
                  <th>Confidence</th>
                  <th>Applied</th>
                  <th>Engine</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td className="mono">{r.payment_id}</td>
                    <td className="mono">{r.invoice_id || '—'}</td>
                    <td>
                      <span className={`badge ${r.status === 'reconciled' ? 'badge-ok' : 'badge-warn'}`}>
                        {r.status === 'reconciled' ? 'Matched' : 'Needs review'}
                      </span>
                    </td>
                    <td>{(r.confidence * 100).toFixed(0)}%</td>
                    <td>{money(r.amount_applied)}</td>
                    <td>
                      <span className="badge badge-muted">{r.source || r.tier || '—'}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
