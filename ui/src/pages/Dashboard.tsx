import { useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import DashboardQuery from '../components/DashboardQuery'
import { money, pct, STAGE_ORDER } from '../lib/format'
import { useRun } from '../state/RunContext'

export default function DashboardPage() {
  const {
    metrics,
    stages,
    running,
    exceptions,
    cash,
    invoices,
    payments,
    runRecon,
    runId,
    bankPdfPath,
    uploadBankPdf,
    setError,
  } = useRun()
  const pdfInputRef = useRef<HTMLInputElement>(null)
  const [uploadingPdf, setUploadingPdf] = useState(false)
  const [workspaceActive, setWorkspaceActive] = useState(false)
  const pending = Math.max(
    exceptions.filter((e) => e.status === 'pending').length,
    typeof metrics?.pending_review === 'number' ? metrics.pending_review : 0,
  )
  const throughput = metrics?.details?.throughput_per_sec
  const duration = metrics?.details?.duration_sec

  async function onPdfPick(file: File | null) {
    if (!file) return
    setUploadingPdf(true)
    try {
      await uploadBankPdf(file)
    } catch (e) {
      setError(String(e))
    } finally {
      setUploadingPdf(false)
      if (pdfInputRef.current) pdfInputRef.current.value = ''
    }
  }

  const stageMap = useMemo(() => {
    const map: Record<string, string> = {}
    for (const s of stages) map[s.name] = s.status
    return map
  }, [stages])

  const chartData = useMemo(() => {
    if (!metrics) return []
    return [
      { name: 'Accuracy', value: metrics.accuracy },
      { name: 'Precision', value: metrics.precision },
      { name: 'Recall', value: metrics.recall },
      { name: 'F1', value: metrics.f1 },
      { name: 'Match', value: metrics.match_rate },
    ]
      .filter((row) => row.value != null && !Number.isNaN(Number(row.value)))
      .map((row) => ({ name: row.name, value: Number(row.value) * 100 }))
  }, [metrics])

  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>Dashboard</h1>
          <p>
            Intent-driven finance workspace over ERP, bank, and payment processor data — ask in
            natural language, review exceptions, and close the loop.
          </p>
        </div>
        <div className="row-actions" style={{ alignItems: 'center' }}>
          <input
            ref={pdfInputRef}
            type="file"
            accept="application/pdf,.pdf"
            hidden
            onChange={(e) => onPdfPick(e.target.files?.[0] || null)}
          />
          <button
            type="button"
            className="btn btn-secondary"
            disabled={running || uploadingPdf}
            onClick={() => pdfInputRef.current?.click()}
            title="Optional bank PDF for OCR intake"
          >
            {uploadingPdf ? 'Uploading…' : bankPdfPath ? `PDF: ${bankPdfPath}` : 'Upload bank PDF'}
          </button>
          <button className="btn btn-primary" onClick={runRecon} disabled={running}>
            {running ? 'Running pipeline…' : runId ? 'Re-run reconciliation' : 'Start reconciliation'}
          </button>
        </div>
      </div>

      <DashboardQuery onWorkspaceActive={setWorkspaceActive} />

      {!runId && (
        <div className="card card-pad">
          <div className="empty">
            <strong>No reconciliation run yet</strong>
            Start a run or ask the workspace to reconcile — demo invoices and payments will load
            automatically.
          </div>
        </div>
      )}

      {workspaceActive ? (
        <details className="ws-baseline">
          <summary>Baseline batch overview</summary>
          <BaselineOverview
            metrics={metrics}
            pending={pending}
            throughput={throughput}
            duration={duration}
            stageMap={stageMap}
            running={running}
            chartData={chartData}
            invoices={invoices.length || cash?.invoice_count || 0}
            payments={payments.length || cash?.payment_count || 0}
            cash={cash?.cash_applied}
          />
        </details>
      ) : (
        <BaselineOverview
          metrics={metrics}
          pending={pending}
          throughput={throughput}
          duration={duration}
          stageMap={stageMap}
          running={running}
          chartData={chartData}
          invoices={invoices.length || cash?.invoice_count || 0}
          payments={payments.length || cash?.payment_count || 0}
          cash={cash?.cash_applied}
        />
      )}
    </div>
  )
}

function BaselineOverview({
  metrics,
  pending,
  throughput,
  duration,
  stageMap,
  running,
  chartData,
  invoices,
  payments,
  cash,
}: {
  metrics: ReturnType<typeof useRun>['metrics']
  pending: number
  throughput: number | undefined
  duration: number | undefined
  stageMap: Record<string, string>
  running: boolean
  chartData: Array<{ name: string; value: number }>
  invoices: number
  payments: number
  cash: number | null | undefined
}) {
  return (
    <div className="stack">
      <div className="grid-5">
        <div className="stat">
          <div className="label">Match rate</div>
          <div className="value">{pct(metrics?.match_rate)}</div>
          <div className="meta">Auto-closed payments</div>
        </div>
        <div className="stat">
          <div className="label">Accuracy</div>
          <div className="value">{pct(metrics?.accuracy)}</div>
          <div className="meta">Vs labeled ground truth</div>
        </div>
        <div className="stat">
          <div className="label">F1 score</div>
          <div className="value">{pct(metrics?.f1)}</div>
          <div className="meta">Precision / recall balance</div>
        </div>
        <div className="stat">
          <div className="label">Throughput</div>
          <div className="value" style={{ fontSize: 22 }}>
            {throughput != null ? `${throughput}/s` : '—'}
          </div>
          <div className="meta">{duration != null ? `${duration}s total` : 'Payments per second'}</div>
        </div>
        <div className="stat">
          <div className="label">Open reviews</div>
          <div className="value">{pending}</div>
          <div className="meta">Needs human decision</div>
        </div>
      </div>

      <div className="card card-pad">
        <h2 className="card-title">Pipeline</h2>
        <p className="card-sub">Intake through journals, evaluation, and report</p>
        <div className="pipeline" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(110px, 1fr))' }}>
          {STAGE_ORDER.map((name) => {
            const st = stageMap[name] || (running ? 'pending' : 'idle')
            return (
              <div key={name} className={`step ${st}`}>
                <div className="name">{name.replace('_', ' ')}</div>
                <div className="st">{st}</div>
              </div>
            )
          })}
        </div>
      </div>

      <div className="grid-2">
        <div className="card card-pad">
          <h2 className="card-title">Quality scorecard</h2>
          <p className="card-sub">
            {metrics
              ? `${metrics.auto_reconciled ?? 0} of ${metrics.total_payments ?? 0} payments auto-reconciled · unreconciled ${money(metrics.unreconciled_amount)}`
              : 'Run reconciliation to populate metrics'}
          </p>
          {chartData.length > 0 ? (
            <div style={{ height: 220 }}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                  <XAxis dataKey="name" tick={{ fill: '#64748b', fontSize: 11 }} />
                  <YAxis unit="%" domain={[0, 100]} tick={{ fill: '#64748b', fontSize: 11 }} />
                  <Tooltip formatter={(v) => [`${Number(v).toFixed(1)}%`, 'Score']} />
                  <Bar dataKey="value" fill="#0f766e" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <div className="empty">No metrics yet</div>
          )}
        </div>

        <div className="card card-pad">
          <h2 className="card-title">Workspace snapshot</h2>
          <p className="card-sub">Current batch loaded into this demo workspace</p>
          <div className="stack" style={{ gap: 10 }}>
            <Link to="/invoices" className="source-card" style={{ textDecoration: 'none' }}>
              <strong>Invoices</strong>
              <div className="meta">{invoices} open AR records from QuickBooks</div>
            </Link>
            <Link to="/payments" className="source-card" style={{ textDecoration: 'none' }}>
              <strong>Payments</strong>
              <div className="meta">{payments} receipts from bank + processor</div>
            </Link>
            <Link to="/cash" className="source-card" style={{ textDecoration: 'none' }}>
              <strong>Cash applied</strong>
              <div className="meta">{money(cash)} posted against invoices</div>
            </Link>
            <Link to="/reviews" className="source-card" style={{ textDecoration: 'none' }}>
              <strong>Reviews</strong>
              <div className="meta">{pending} exceptions waiting for approval</div>
            </Link>
          </div>
        </div>
      </div>
    </div>
  )
}
