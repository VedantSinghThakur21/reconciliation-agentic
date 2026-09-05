import { useMemo, useState } from 'react'
import { money } from '../lib/format'
import { useRun } from '../state/RunContext'

export default function PaymentsPage() {
  const { payments, results, runId } = useRun()
  const [q, setQ] = useState('')

  const matchByPayment = useMemo(() => {
    const map = new Map<string, { status: string; invoiceId?: string; confidence: number; source?: string }>()
    for (const r of results) {
      if (!r.payment_id) continue
      map.set(r.payment_id, {
        status: r.status,
        invoiceId: r.invoice_id,
        confidence: r.confidence,
        source: r.source,
      })
    }
    return map
  }, [results])

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return payments.filter((p) => {
      if (!needle) return true
      return [p.external_id, p.party_name, p.reference, p.source]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(needle))
    })
  }, [payments, q])

  const matched = rows.filter((p) => matchByPayment.get(p.external_id)?.status === 'reconciled').length

  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>Payments</h1>
          <p>Incoming cash from bank and payment processor feeds. The agent matches each receipt to an invoice when evidence is strong enough.</p>
        </div>
      </div>

      <div className="grid-3">
        <div className="stat">
          <div className="label">Total payments</div>
          <div className="value">{payments.length}</div>
        </div>
        <div className="stat">
          <div className="label">Auto matched</div>
          <div className="value">{matched}</div>
        </div>
        <div className="stat">
          <div className="label">Not auto-closed</div>
          <div className="value">{Math.max(rows.length - matched, 0)}</div>
        </div>
      </div>

      <div className="card">
        <div className="card-pad" style={{ borderBottom: '1px solid var(--line)', display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center' }}>
          <div>
            <h2 className="card-title">Payment feed</h2>
            <p className="card-sub" style={{ marginBottom: 0 }}>Bank + processor settlements in this run</p>
          </div>
          <input className="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search payments…" />
        </div>
        {!runId || payments.length === 0 ? (
          <div className="empty">
            <strong>No payments loaded</strong>
            Run reconciliation to ingest bank and processor demo feeds.
          </div>
        ) : (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Payment</th>
                  <th>Payer</th>
                  <th>Amount</th>
                  <th>Date</th>
                  <th>Reference</th>
                  <th>Channel</th>
                  <th>Status</th>
                  <th>Invoice</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((p) => {
                  const match = matchByPayment.get(p.external_id)
                  const status = match?.status || 'unreconciled'
                  return (
                    <tr key={p.id}>
                      <td className="mono">{p.external_id}</td>
                      <td>{p.party_name || '—'}</td>
                      <td>{money(p.amount)}</td>
                      <td>{p.txn_date || '—'}</td>
                      <td className="mono">{p.reference || '—'}</td>
                      <td>
                        <span className="badge badge-muted">{p.source}</span>
                      </td>
                      <td>
                        <span
                          className={`badge ${
                            status === 'reconciled'
                              ? 'badge-ok'
                              : status === 'pending_review'
                                ? 'badge-warn'
                                : 'badge-muted'
                          }`}
                        >
                          {status === 'reconciled' ? 'Matched' : status === 'pending_review' ? 'Needs review' : 'Unmatched'}
                        </span>
                      </td>
                      <td className="mono">{match?.invoiceId || '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
