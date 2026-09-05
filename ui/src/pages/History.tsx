import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { pct } from '../lib/format'
import { useRun } from '../state/RunContext'

type RunRow = Awaited<ReturnType<typeof api.listRuns>>['runs'][number]

export default function HistoryPage() {
  const { runId, selectRun, loadLatest, running } = useRun()
  const [runs, setRuns] = useState<RunRow[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const res = await api.listRuns(100)
      setRuns(res.runs || [])
      setActiveId(res.active_run_id)
      setError(null)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh, runId])

  async function openRun(id: string) {
    await selectRun(id)
  }

  async function goToActive() {
    await loadLatest()
  }

  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>History</h1>
          <p>
            Every reconciliation keeps its own run_id, metrics, exceptions, audit, and report. Opening a
            run loads only that run’s data — prior runs stay intact.
          </p>
        </div>
        <div className="row-actions">
          <button type="button" className="btn btn-secondary" onClick={() => void refresh()} disabled={loading}>
            Refresh list
          </button>
          <button type="button" className="btn btn-primary" onClick={() => void goToActive()} disabled={running}>
            Open active run
          </button>
        </div>
      </div>

      {error ? <div className="error-banner">{error}</div> : null}

      <div className="card card-pad">
        <h2 className="card-title">All runs</h2>
        <p className="card-sub">
          Viewing workspace: <span className="mono">{runId || '—'}</span>
          {activeId && runId !== activeId ? (
            <>
              {' '}
              · newest completed is <span className="mono">{activeId}</span>
            </>
          ) : null}
        </p>

        {loading && runs.length === 0 ? (
          <div className="empty">Loading runs…</div>
        ) : runs.length === 0 ? (
          <div className="empty">
            <strong>No runs yet</strong>
            Start reconciliation from the Dashboard to create the first run.
          </div>
        ) : (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Status</th>
                  <th>Started</th>
                  <th>Match</th>
                  <th>Accuracy</th>
                  <th>Exceptions</th>
                  <th>PDF</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => {
                  const preview = r.summary_preview || {}
                  const isViewing = r.id === runId
                  const isActive = r.id === activeId
                  return (
                    <tr key={r.id} className={isViewing ? 'ws-row-active' : undefined}>
                      <td>
                        <div className="mono">{r.id}</div>
                        <div className="meta" style={{ maxWidth: 280 }}>
                          {(r.user_request || '').slice(0, 80)}
                          {(r.user_request || '').length > 80 ? '…' : ''}
                        </div>
                        {isActive ? <span className="badge badge-brand">Active</span> : null}{' '}
                        {isViewing ? <span className="badge badge-ok">Viewing</span> : null}
                      </td>
                      <td>
                        <span className={`badge ${r.status === 'completed' ? 'badge-ok' : 'badge-muted'}`}>
                          {r.status}
                        </span>
                      </td>
                      <td className="mono" style={{ fontSize: 11 }}>
                        {r.started_at}
                      </td>
                      <td>{pct(preview.match_rate)}</td>
                      <td>{pct(preview.accuracy)}</td>
                      <td>{preview.exception_count ?? '—'}</td>
                      <td className="mono" style={{ fontSize: 11 }}>
                        {r.bank_pdf_path ? String(r.bank_pdf_path).split(/[/\\]/).pop() : '—'}
                      </td>
                      <td>
                        <div className="row-actions">
                          <button
                            type="button"
                            className="btn btn-sm btn-secondary"
                            disabled={isViewing || running}
                            onClick={() => void openRun(r.id)}
                          >
                            {isViewing ? 'Open' : 'Open'}
                          </button>
                          <Link className="btn btn-sm btn-secondary" to="/">
                            Dashboard
                          </Link>
                        </div>
                      </td>
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
