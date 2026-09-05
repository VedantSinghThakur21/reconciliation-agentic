import { useMemo, useState } from 'react'
import { useRun } from '../state/RunContext'

const ACTIONS = ['APPROVE', 'REJECT', 'RESOLVE', 'ESCALATE', 'WRITE_OFF'] as const

export default function ReviewsPage() {
  const { exceptions, resolve, runId } = useRun()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [tab, setTab] = useState<'pending' | 'all'>('pending')

  const rows = useMemo(() => {
    if (tab === 'pending') return exceptions.filter((e) => e.status === 'pending')
    return exceptions
  }, [exceptions, tab])

  const selected = exceptions.find((e) => e.id === selectedId) || null
  const pendingCount = exceptions.filter((e) => e.status === 'pending').length

  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>Reviews</h1>
          <p>
            Honest exception queue — payments the agent could not safely auto-close.
            AI may recommend an action; human_decision stays null until you click.
          </p>
        </div>
      </div>

      <div className="grid-3">
        <div className="stat">
          <div className="label">Pending</div>
          <div className="value">{pendingCount}</div>
        </div>
        <div className="stat">
          <div className="label">Total exceptions</div>
          <div className="value">{exceptions.length}</div>
        </div>
        <div className="stat">
          <div className="label">Resolved</div>
          <div className="value">{exceptions.length - pendingCount}</div>
        </div>
      </div>

      <div className="card card-pad">
        <div className="row-actions" style={{ marginBottom: 14 }}>
          <button
            className={`btn btn-sm ${tab === 'pending' ? 'btn-primary' : 'btn-secondary'}`}
            onClick={() => setTab('pending')}
          >
            Pending
          </button>
          <button
            className={`btn btn-sm ${tab === 'all' ? 'btn-primary' : 'btn-secondary'}`}
            onClick={() => setTab('all')}
          >
            All
          </button>
        </div>

        {!runId || exceptions.length === 0 ? (
          <div className="empty">
            <strong>No exceptions</strong>
            Run reconciliation first. Unmatched, partial, and duplicate cases land here.
          </div>
        ) : rows.length === 0 ? (
          <div className="empty">
            <strong>Queue clear</strong>
            No pending reviews in this run.
          </div>
        ) : (
          rows.map((ex) => (
            <div className="exception" key={ex.id}>
              <h3>
                <span className="mono">{ex.payment_id}</span>
                {ex.invoice_id ? <> → <span className="mono">{ex.invoice_id}</span></> : null}
              </h3>
              <p>
                <span
                  className={`badge ${
                    ex.status === 'pending'
                      ? 'badge-warn'
                      : ex.status === 'confirmed' || ex.status === 'resolved'
                        ? 'badge-ok'
                        : 'badge-muted'
                  }`}
                >
                  {ex.status}
                </span>{' '}
                {ex.reason} · {(ex.confidence * 100).toFixed(0)}% confidence · {ex.id}
              </p>
              <p className="card-sub">
                human_decision:{' '}
                {ex.status === 'pending' ? (
                  <span className="badge badge-warn">null</span>
                ) : (
                  <span className="badge badge-ok">{ex.status}</span>
                )}
              </p>
              {selected?.id === ex.id && <p>{ex.reasoning}</p>}
              <div className="row-actions">
                <button className="btn btn-secondary btn-sm" onClick={() => setSelectedId(ex.id)}>
                  View evidence
                </button>
                {ex.status === 'pending' &&
                  ACTIONS.map((a) => (
                    <button
                      key={a}
                      className={`btn btn-sm ${a === 'APPROVE' ? 'btn-ok' : a === 'REJECT' ? 'btn-danger' : 'btn-secondary'}`}
                      onClick={() => resolve(ex.id, a)}
                    >
                      {a}
                    </button>
                  ))}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
