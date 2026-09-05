import { money } from '../lib/format'
import { useRun } from '../state/RunContext'

export default function CashPage() {
  const { cash, metrics, runId } = useRun()

  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>Cash position</h1>
          <p>
            Snapshot of applied cash vs remaining receivables after this reconciliation batch —
            “run the books and the cash position.”
          </p>
        </div>
      </div>

      {!runId ? (
        <div className="card card-pad">
          <div className="empty">
            <strong>No cash snapshot yet</strong>
            Run reconciliation to compute applied cash, open AR, and unapplied receipts.
          </div>
        </div>
      ) : (
        <>
          <div className="grid-3">
            <div className="stat">
              <div className="label">Cash applied</div>
              <div className="value" style={{ fontSize: 22 }}>{money(cash?.cash_applied)}</div>
              <div className="meta">Posted to invoices</div>
            </div>
            <div className="stat">
              <div className="label">Open AR</div>
              <div className="value" style={{ fontSize: 22 }}>{money(cash?.open_ar)}</div>
              <div className="meta">Still outstanding</div>
            </div>
            <div className="stat">
              <div className="label">Unapplied cash</div>
              <div className="value" style={{ fontSize: 22 }}>{money(cash?.unapplied_cash)}</div>
              <div className="meta">Received but not matched</div>
            </div>
          </div>

          <div className="grid-3">
            <div className="stat">
              <div className="label">Net exposure</div>
              <div className="value" style={{ fontSize: 22 }}>{money(cash?.net_exposure)}</div>
              <div className="meta">Open AR − unapplied cash</div>
            </div>
            <div className="stat">
              <div className="label">Invoices in batch</div>
              <div className="value">{cash?.invoice_count ?? '—'}</div>
            </div>
            <div className="stat">
              <div className="label">Payments in batch</div>
              <div className="value">{cash?.payment_count ?? metrics?.total_payments ?? '—'}</div>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
