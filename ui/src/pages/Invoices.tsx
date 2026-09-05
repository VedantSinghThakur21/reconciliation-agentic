import { useMemo, useState } from 'react'
import { money } from '../lib/format'
import { useRun } from '../state/RunContext'

export default function InvoicesPage() {
  const { invoices, results, runId } = useRun()
  const [q, setQ] = useState('')

  const matchByInvoice = useMemo(() => {
    const map = new Map<string, { status: string; paymentId?: string; confidence: number }>()
    for (const r of results) {
      if (!r.invoice_id) continue
      const prev = map.get(r.invoice_id)
      if (!prev || r.status === 'reconciled' || r.confidence >= (prev.confidence || 0)) {
        map.set(r.invoice_id, {
          status: r.status,
          paymentId: r.payment_id,
          confidence: r.confidence,
        })
      }
    }
    return map
  }, [results])

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return invoices.filter((inv) => {
      if (!needle) return true
      return [inv.external_id, inv.party_name, inv.reference, String(inv.amount)]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(needle))
    })
  }, [invoices, q])

  const reconciled = rows.filter((r) => matchByInvoice.get(r.external_id)?.status === 'reconciled').length

  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>Invoices</h1>
          <p>Accounts receivable from QuickBooks. Each row is an open invoice the agent tries to settle with incoming cash.</p>
        </div>
      </div>

      <div className="grid-3">
        <div className="stat">
          <div className="label">Total invoices</div>
          <div className="value">{invoices.length}</div>
        </div>
        <div className="stat">
          <div className="label">Matched in view</div>
          <div className="value">{reconciled}</div>
        </div>
        <div className="stat">
          <div className="label">Still open</div>
          <div className="value">{Math.max(rows.length - reconciled, 0)}</div>
        </div>
      </div>

      <div className="card">
        <div className="card-pad" style={{ borderBottom: '1px solid var(--line)', display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center' }}>
          <div>
            <h2 className="card-title">Invoice register</h2>
            <p className="card-sub" style={{ marginBottom: 0 }}>Filter by invoice id, customer, or reference</p>
          </div>
          <input className="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search invoices…" />
        </div>
        {!runId || invoices.length === 0 ? (
          <div className="empty">
            <strong>No invoices loaded</strong>
            Run reconciliation from the top bar to ingest the QuickBooks demo batch.
          </div>
        ) : (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Invoice</th>
                  <th>Customer</th>
                  <th>Amount</th>
                  <th>Date</th>
                  <th>Reference</th>
                  <th>Status</th>
                  <th>Matched payment</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((inv) => {
                  const match = matchByInvoice.get(inv.external_id)
                  const status = match?.status || 'open'
                  return (
                    <tr key={inv.id}>
                      <td className="mono">{inv.external_id}</td>
                      <td>{inv.party_name || '—'}</td>
                      <td>{money(inv.amount)}</td>
                      <td>{inv.txn_date || '—'}</td>
                      <td className="mono">{inv.reference || '—'}</td>
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
                          {status === 'reconciled' ? 'Settled' : status === 'pending_review' ? 'In review' : 'Open'}
                        </span>
                      </td>
                      <td className="mono">{match?.paymentId || '—'}</td>
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
