import { useState, type ReactNode } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { money, pct } from '../lib/format'
import {
  HUMAN_ACTIONS,
  isAllowedComponentType,
  type UIComponent,
  type WorkspaceUI,
} from './schema'

type ResolveFn = (exceptionId: string, decision: string) => Promise<void>

function formatValue(label: string, value: unknown, meta?: unknown): string {
  const l = String(label || '').toLowerCase()
  const m = String(meta || '').toLowerCase()
  if (typeof value === 'number') {
    if (l.includes('rate') || l.includes('accuracy') || l.includes('precision') || l.includes('recall') || l.includes('f1') || l.includes('score') || l.includes('conf')) {
      if (value <= 1) return pct(value)
      return `${value.toFixed(1)}%`
    }
    if (l.includes('amount') || l.includes('cash') || l.includes('ar') || m.includes('inr')) {
      return money(value)
    }
    return Number.isInteger(value) ? String(value) : value.toLocaleString('en-IN')
  }
  if (value == null) return '—'
  return String(value)
}

function EmptyState({ title, message }: { title?: string; message?: string }) {
  return (
    <div className="ws-empty" role="status">
      {title ? <strong>{title}</strong> : null}
      <span>{message || 'Nothing to show for this view.'}</span>
    </div>
  )
}

function Section({
  title,
  subtitle,
  children,
  className = '',
}: {
  title?: string | null
  subtitle?: string | null
  children: ReactNode
  className?: string
}) {
  return (
    <section className={`ws-section ${className}`.trim()}>
      {title ? (
        <header className="ws-section-head">
          <h3>{title}</h3>
          {subtitle ? <p>{subtitle}</p> : null}
        </header>
      ) : null}
      {children}
    </section>
  )
}

function KpiCard({ data }: { data: Record<string, unknown> }) {
  const tone = String(data.tone || 'neutral')
  return (
    <div className={`ws-kpi tone-${tone}`}>
      <div className="label">{String(data.label || 'KPI')}</div>
      <div className="value">{formatValue(String(data.label || ''), data.value, data.meta)}</div>
      {data.meta ? <div className="meta">{String(data.meta)}</div> : null}
    </div>
  )
}

function MetricGrid({ data }: { data: Record<string, unknown> }) {
  const metrics = (data.metrics as Array<{ label: string; value: unknown }>) || []
  if (data.empty || metrics.length === 0) {
    return <EmptyState title={String(data.title || 'Metrics')} message={String(data.message || '')} />
  }
  return (
    <Section title={String(data.title || 'Metrics')}>
      <div className={`ws-kpi-grid cols-${Math.min(metrics.length, 5)}`}>
        {metrics.map((m) => (
          <KpiCard key={m.label} data={{ label: m.label, value: m.value }} />
        ))}
      </div>
    </Section>
  )
}

function ExceptionTable({
  data,
  onResolve,
  resolvingId,
}: {
  data: Record<string, unknown>
  onResolve?: ResolveFn
  resolvingId?: string | null
}) {
  const rows = (data.rows as Array<Record<string, unknown>>) || []
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selected = rows.find((r) => String(r.id || r.transaction_id) === selectedId) || null
  if (data.empty || rows.length === 0) {
    return (
      <Section title={String(data.title || 'Needs Your Attention')}>
        <EmptyState message={String(data.message || 'No exceptions need attention.')} />
      </Section>
    )
  }
  return (
    <Section
      title={String(data.title || 'Needs Your Attention')}
      subtitle={`${rows.length} item${rows.length === 1 ? '' : 's'} awaiting a human decision`}
    >
      <div className="table-wrap ws-table">
        <table className="data">
          <thead>
            <tr>
              <th>Transaction</th>
              <th>Amount</th>
              <th>Source</th>
              <th>Reason</th>
              <th>Conf.</th>
              <th>Exception</th>
              <th>AI recommendation</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const rowId = String(r.id || r.transaction_id || '')
              const active = selectedId === rowId
              const pending = r.human_decision == null
              return (
                <tr
                  key={rowId}
                  className={`ws-click-row${active ? ' is-selected' : ''}`}
                  onClick={() => setSelectedId(active ? null : rowId)}
                >
                  <td className="mono">{String(r.transaction_id || '—')}</td>
                  <td>{money(Number(r.amount || 0))}</td>
                  <td>{String(r.source || '—')}</td>
                  <td>{String(r.reason || r.exception_type || '—')}</td>
                  <td>
                    {r.confidence != null ? `${(Number(r.confidence) * 100).toFixed(0)}%` : '—'}
                  </td>
                  <td>
                    <span className="badge badge-muted">{String(r.exception_type || '—')}</span>
                  </td>
                  <td>
                    <span className="badge badge-brand">{String(r.ai_recommendation || '—')}</span>
                  </td>
                  <td>
                    {pending ? (
                      <span className="badge badge-warn">{String(r.status || 'HUMAN_REVIEW')}</span>
                    ) : (
                      <span className="badge badge-ok">{String(r.human_decision)}</span>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {selected ? (
        <div className="ws-pending-detail">
          <header>
            <h4>
              <span className="mono">{String(selected.transaction_id)}</span>
            </h4>
            <p>AI recommendation is a suggestion only — human_decision stays unset until you act.</p>
          </header>
          <div className="ws-detail-grid">
            {[
              { label: 'Transaction', value: selected.transaction_id, mono: true },
              { label: 'Amount', value: money(Number(selected.amount || 0)) },
              { label: 'Source', value: selected.source || '—' },
              { label: 'Reason', value: selected.reason || '—' },
              {
                label: 'Confidence',
                value:
                  selected.confidence != null
                    ? `${(Number(selected.confidence) * 100).toFixed(0)}%`
                    : '—',
              },
              { label: 'Exception type', value: selected.exception_type || '—' },
              { label: 'AI recommendation', value: selected.ai_recommendation || '—' },
              {
                label: 'Human decision',
                value: selected.human_decision == null ? 'null' : String(selected.human_decision),
              },
              { label: 'Status', value: selected.status || (selected.human_decision == null ? 'HUMAN_REVIEW' : 'DECIDED') },
            ].map((f) => (
              <div key={f.label} className="ws-detail-cell">
                <div className="label">{f.label}</div>
                <div className={`value ${f.mono ? 'mono' : ''}`.trim()}>{String(f.value)}</div>
              </div>
            ))}
          </div>
          {selected.human_decision == null && selected.id && onResolve ? (
            <div className="row-actions" style={{ marginTop: 12 }}>
              {HUMAN_ACTIONS.map((a) => {
                const busy = resolvingId != null && resolvingId === String(selected.id)
                return (
                  <button
                    key={a}
                    type="button"
                    className={`btn btn-sm ${a === 'APPROVE' ? 'btn-ok' : a === 'REJECT' ? 'btn-danger' : 'btn-secondary'}`}
                    disabled={busy || resolvingId != null}
                    onClick={(ev) => {
                      ev.stopPropagation()
                      onResolve(String(selected.id), a)
                    }}
                  >
                    {busy ? '…' : a.replace('_', ' ')}
                  </button>
                )
              })}
            </div>
          ) : null}
        </div>
      ) : (
        <p className="ws-reco-note">Click an item to open details and record a human decision.</p>
      )}
    </Section>
  )
}

function TransactionTable({ data }: { data: Record<string, unknown> }) {
  const rows = (data.rows as Array<Record<string, unknown>>) || []
  if (data.empty || rows.length === 0) {
    return (
      <Section title={String(data.title || 'Amount mismatches')}>
        <EmptyState message={String(data.message || 'No mismatches found.')} />
      </Section>
    )
  }
  return (
    <Section title={String(data.title || 'Amount mismatches')} subtitle={`${rows.length} mismatch${rows.length === 1 ? '' : 'es'}`}>
      <div className="table-wrap ws-table">
        <table className="data">
          <thead>
            <tr>
              <th>Payment</th>
              <th>Matched invoice</th>
              <th>Payment amt</th>
              <th>ERP amt</th>
              <th>Delta</th>
              <th>Amount score</th>
              <th>Conf.</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={String(r.id || r.transaction_id)}>
                <td className="mono">{String(r.transaction_id || '—')}</td>
                <td className="mono">{String(r.matched_txn_id || '—')}</td>
                <td>{r.counter_amount != null ? money(Number(r.counter_amount)) : '—'}</td>
                <td>{r.erp_amount != null ? money(Number(r.erp_amount)) : '—'}</td>
                <td className="ws-delta">
                  {r.mismatch_amount != null ? money(Number(r.mismatch_amount)) : '—'}
                </td>
                <td>
                  {r.amount_score != null ? `${(Number(r.amount_score) * 100).toFixed(0)}%` : '—'}
                </td>
                <td>
                  {r.confidence != null ? `${(Number(r.confidence) * 100).toFixed(0)}%` : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  )
}

function ScoreBars({ fields }: { fields: Record<string, unknown> }) {
  const scoreKeys = ['confidence', 'amount_score', 'merchant_score', 'date_score', 'reference_score']
  return (
    <div className="ws-scores">
      {scoreKeys.map((k) => {
        const raw = fields[k]
        if (raw == null) return null
        const n = Number(raw)
        const pctVal = n <= 1 ? n * 100 : n
        return (
          <div key={k} className="score-bar">
            <div className="score-bar-label">
              <span>{k.replace('_', ' ').replace(' score', '')}</span>
              <span>{pctVal.toFixed(0)}%</span>
            </div>
            <div className="score-bar-track">
              <div className="score-bar-fill" style={{ width: `${Math.max(0, Math.min(100, pctVal))}%` }} />
            </div>
          </div>
        )
      })}
      {Object.entries(fields)
        .filter(([k]) => !scoreKeys.includes(k))
        .map(([k, v]) => (
          <div key={k} className="ws-field-row">
            <span>{k}</span>
            <strong>{v == null ? '—' : String(v)}</strong>
          </div>
        ))}
    </div>
  )
}

function ComparisonPanel({ data }: { data: Record<string, unknown> }) {
  const rows = (data.rows as Array<Record<string, unknown>>) || []
  if (data.empty || rows.length === 0) {
    return (
      <Section title={String(data.title || 'Comparison')}>
        <EmptyState message={String(data.message || 'No comparisons available.')} />
      </Section>
    )
  }
  return (
    <Section title={String(data.title || 'Comparison')}>
      <div className="ws-compare-grid">
        {rows.map((r, i) => (
          <article key={i} className="ws-compare-card">
            <header>
              <span className="mono">{String(r.left || '—')}</span>
              <span className="ws-compare-arrow" aria-hidden>
                ↔
              </span>
              <span className="mono">{String(r.right || '—')}</span>
            </header>
            <ScoreBars fields={(r.fields as Record<string, unknown>) || {}} />
          </article>
        ))}
      </div>
    </Section>
  )
}

function TransactionDetail({ data }: { data: Record<string, unknown> }) {
  const fields = [
    { label: 'Transaction', value: data.transaction_id, mono: true },
    { label: 'Amount', value: data.amount != null ? money(Number(data.amount)) : '—' },
    { label: 'Currency', value: data.currency || '—' },
    { label: 'Merchant', value: data.merchant || '—' },
    { label: 'Date', value: data.date || '—' },
    { label: 'Reference', value: data.reference || '—', mono: true },
    { label: 'Source', value: data.source || '—' },
    { label: 'Exception', value: data.exception_type || '—' },
    { label: 'AI suggestion', value: data.ai_recommendation || '—' },
    {
      label: 'Decision',
      value: data.human_decision == null ? 'Pending' : String(data.human_decision),
      badge: data.human_decision == null ? 'warn' : 'ok',
    },
  ]
  return (
    <Section title={String(data.title || 'Transaction detail')}>
      <div className="ws-detail-grid">
        {fields.map((f) => (
          <div key={f.label} className="ws-detail-cell">
            <div className="label">{f.label}</div>
            <div className={`value ${f.mono ? 'mono' : ''}`.trim()}>
              {'badge' in f && f.badge ? (
                <span className={`badge badge-${f.badge}`}>{String(f.value)}</span>
              ) : (
                String(f.value)
              )}
            </div>
          </div>
        ))}
      </div>
    </Section>
  )
}

function RecommendationPanel({
  data,
  onResolve,
  resolvingId,
}: {
  data: Record<string, unknown>
  onResolve?: ResolveFn
  resolvingId?: string | null
}) {
  const actions = (data.actions as string[]) || []
  const exceptionId = data.exception_id != null ? String(data.exception_id) : null
  const busy = exceptionId != null && resolvingId === exceptionId
  if (data.empty) {
    return (
      <Section title={String(data.title || 'Recommendations')}>
        <EmptyState message={String(data.message || 'No recommendations.')} />
      </Section>
    )
  }
  return (
    <Section title={String(data.title || 'Recommendations')}>
      <div className="ws-reco">
        {data.reason ? <p className="ws-reco-reason">{String(data.reason)}</p> : null}
        {actions.length > 0 ? (
          <div className="chips">
            {actions.map((a) => (
              <span key={a} className="chip ws-chip-static">
                Suggested: {a}
              </span>
            ))}
          </div>
        ) : null}
        {data.note ? <p className="ws-reco-note">{String(data.note)}</p> : null}
        {exceptionId && data.human_decision == null && onResolve ? (
          <div className="row-actions" style={{ marginTop: 12 }}>
            {HUMAN_ACTIONS.map((a) => (
              <button
                key={a}
                type="button"
                className={`btn btn-sm ${a === 'APPROVE' ? 'btn-ok' : 'btn-secondary'}`}
                disabled={busy || resolvingId != null}
                onClick={() => onResolve(exceptionId, a)}
              >
                {busy ? '…' : a.replace('_', ' ')}
              </button>
            ))}
          </div>
        ) : null}
        {data.human_decision != null ? (
          <p className="ws-reco-note">
            Recorded decision: <strong>{String(data.human_decision)}</strong>
          </p>
        ) : null}
      </div>
    </Section>
  )
}

function AgentProgress({ data }: { data: Record<string, unknown> }) {
  const completed = (data.completed as string[]) || []
  const current = String(data.current || 'Working…')
  return (
    <Section title="Working">
      <div className="ws-progress">
        <div className="ws-progress-current">
          <span className="ws-pulse" aria-hidden />
          {current}
        </div>
        {completed.length > 0 ? (
          <ol className="ws-progress-list">
            {completed.map((c) => (
              <li key={c}>
                <span className="ws-check" aria-hidden>
                  ✓
                </span>
                {c}
              </li>
            ))}
          </ol>
        ) : null}
      </div>
    </Section>
  )
}

function AuditTimeline({ data }: { data: Record<string, unknown> }) {
  const events = (data.events as Array<Record<string, unknown>>) || []
  if (data.empty || events.length === 0) {
    return (
      <Section title={String(data.title || 'Audit trail')}>
        <EmptyState message={String(data.message || 'No audit events yet.')} />
      </Section>
    )
  }
  return (
    <Section title={String(data.title || 'Audit trail')} subtitle={`${events.length} recent events`}>
      <ul className="audit-list">
        {events.map((e, i) => (
          <li key={i}>
            <span className="time">{String(e.created_at || '—')}</span>
            <strong>{String(e.event_type || 'event')}</strong>
            <div>{String(e.message || '')}</div>
            {e.entity_id ? <span className="mono" style={{ fontSize: 11 }}>{String(e.entity_id)}</span> : null}
          </li>
        ))}
      </ul>
    </Section>
  )
}

function QualityChart({ data }: { data: Record<string, unknown> }) {
  const rows = (data.rows as Array<{ name: string; value: number }>) || []
  if (!rows.length) {
    return (
      <Section title={String(data.title || 'Quality scorecard')}>
        <EmptyState message="No quality metrics for this run." />
      </Section>
    )
  }
  return (
    <Section title={String(data.title || 'Quality scorecard')}>
      <div className="ws-chart">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={rows} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
            <XAxis dataKey="name" tick={{ fill: '#64748b', fontSize: 11 }} axisLine={false} tickLine={false} />
            <YAxis unit="%" domain={[0, 100]} tick={{ fill: '#64748b', fontSize: 11 }} axisLine={false} tickLine={false} width={40} />
            <Tooltip formatter={(v) => [`${Number(v).toFixed(1)}%`, 'Score']} />
            <Bar dataKey="value" fill="#0f766e" radius={[4, 4, 0, 0]} maxBarSize={48} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </Section>
  )
}

function MessageCard({ data, variant }: { data: Record<string, unknown>; variant: 'clarification' | 'unavailable' }) {
  return (
    <div className={`ws-message ws-message-${variant}`}>
      <strong>{variant === 'clarification' ? 'Need a bit more detail' : 'Unavailable'}</strong>
      <p>{String(data.message || '—')}</p>
    </div>
  )
}

const REGISTRY: Record<
  string,
  (props: {
    data: Record<string, unknown>
    onResolve?: ResolveFn
    resolvingId?: string | null
  }) => ReactNode
> = {
  kpi: ({ data }) => <KpiCard data={data} />,
  metric_grid: ({ data }) => <MetricGrid data={data} />,
  exception_table: ({ data, onResolve, resolvingId }) => (
    <ExceptionTable data={data} onResolve={onResolve} resolvingId={resolvingId} />
  ),
  transaction_table: ({ data }) => <TransactionTable data={data} />,
  comparison: ({ data }) => <ComparisonPanel data={data} />,
  transaction_detail: ({ data }) => <TransactionDetail data={data} />,
  recommendation: ({ data, onResolve, resolvingId }) => (
    <RecommendationPanel data={data} onResolve={onResolve} resolvingId={resolvingId} />
  ),
  agent_progress: ({ data }) => <AgentProgress data={data} />,
  audit_timeline: ({ data }) => <AuditTimeline data={data} />,
  chart: ({ data }) => <QualityChart data={data} />,
  clarification: ({ data }) => <MessageCard data={data} variant="clarification" />,
  unavailable: ({ data }) => <MessageCard data={data} variant="unavailable" />,
}

export function renderRegisteredComponent(
  component: UIComponent,
  onResolve?: ResolveFn,
  resolvingId?: string | null,
): ReactNode {
  if (!isAllowedComponentType(component.type)) {
    return null
  }
  const Comp = REGISTRY[component.type]
  if (!Comp) return null
  return Comp({ data: component.data || {}, onResolve, resolvingId })
}

function WorkspaceSkeleton() {
  return (
    <div className="ws-canvas ws-loading" aria-busy="true" aria-label="Loading workspace">
      <div className="ws-skel-title" />
      <div className="ws-kpi-grid cols-4">
        <div className="ws-skel-block" />
        <div className="ws-skel-block" />
        <div className="ws-skel-block" />
        <div className="ws-skel-block" />
      </div>
      <div className="ws-skel-block tall" />
    </div>
  )
}

export function DynamicWorkspaceView({
  workspace,
  onResolve,
  connectionError,
  agentError,
  running,
  resolvingId,
}: {
  workspace: WorkspaceUI | null
  onResolve?: ResolveFn
  connectionError?: string | null
  agentError?: string | null
  running?: boolean
  resolvingId?: string | null
}) {
  if (connectionError) {
    return (
      <div className="ws-canvas">
        <div className="ws-message ws-message-error" role="alert">
          <strong>Connection issue</strong>
          <p>Couldn’t reach the finance workspace. Check that the API is running, then try again.</p>
        </div>
      </div>
    )
  }
  if (agentError) {
    return (
      <div className="ws-canvas">
        <div className="ws-message ws-message-error" role="alert">
          <strong>Something went wrong</strong>
          <p>{agentError}</p>
        </div>
      </div>
    )
  }
  if (running && !workspace) {
    return <WorkspaceSkeleton />
  }
  if (!workspace) {
    return (
      <div className="ws-canvas">
        <div className="ws-idle">
          <strong>Finance workspace</strong>
          <p>
            Ask about reconciliation, exceptions, mismatches, cash, audit, or a specific transaction.
            The panel updates to match your request.
          </p>
        </div>
      </div>
    )
  }
  if (workspace.status === 'unavailable') {
    return (
      <div className="ws-canvas">
        <header className="ws-header">
          <h2>{workspace.title}</h2>
        </header>
        <MessageCard data={{ message: workspace.message || 'Data unavailable' }} variant="unavailable" />
      </div>
    )
  }
  if (workspace.status === 'clarification') {
    return (
      <div className="ws-canvas">
        <header className="ws-header">
          <h2>{workspace.title}</h2>
        </header>
        <MessageCard data={{ message: workspace.message || '' }} variant="clarification" />
      </div>
    )
  }
  if (workspace.status === 'agent_failure' || workspace.status === 'error') {
    return (
      <div className="ws-canvas">
        <div className="ws-message ws-message-error" role="alert">
          <strong>Something went wrong</strong>
          <p>{workspace.message || 'Please try again.'}</p>
        </div>
      </div>
    )
  }

  const layout = (workspace.layout || 'focused').toLowerCase()
  const kpis = workspace.components.filter((c) => c.type === 'kpi')
  const rest = workspace.components.filter((c) => c.type !== 'kpi')
  const progress = rest.filter((c) => c.type === 'agent_progress')
  const body = rest.filter((c) => c.type !== 'agent_progress')
  const data = (workspace.data || {}) as Record<string, unknown>
  const pendingFromState = (data.pending_review_items as Array<Record<string, unknown>>) || []
  const hasExceptionTable = body.some((c) => c.type === 'exception_table')
  const crewaiExecuted = Boolean(data.crewai_executed)
  const summary = (data.reconciliation_summary as Record<string, unknown>) || {}
  const primaryCols = layout === 'dashboard' ? Math.min(Math.max(kpis.length, 1), 5) : Math.min(Math.max(kpis.length, 1), 4)

  return (
    <div className={`ws-canvas layout-${layout}`} data-layout={layout}>
      <header className="ws-header">
        <div>
          <h2>{workspace.title}</h2>
          {workspace.reasoning ? <p className="ws-lede">{workspace.reasoning}</p> : null}
          {workspace.run_id ? (
            <p className="ws-run-meta">
              Run <span className="mono">{workspace.run_id}</span>
              {typeof data.kickoff_id === 'string' && data.kickoff_id ? (
                <>
                  {' '}
                  · Kickoff <span className="mono">{String(data.kickoff_id).slice(0, 8)}…</span>
                </>
              ) : null}
              {crewaiExecuted
                ? data.engine === 'crewai-amp' || data.source_of_truth === 'crewai-amp'
                  ? ' · CrewAI Enterprise executed this run'
                  : ' · CrewAI local flow executed this run'
                : ' · Showing stored run data'}
              {typeof data.pending_human_count === 'number' ? (
                <>
                  {' '}
                  · {Number(data.pending_human_count)} pending
                  {typeof summary.auto_matched === 'number' ? ` · ${Number(summary.auto_matched)} auto-matched` : ''}
                  {typeof summary.approved === 'number' ? ` · ${Number(summary.approved)} approved` : ''}
                </>
              ) : null}
            </p>
          ) : null}
        </div>
        {running ? (
          <span className="ws-live" aria-live="polite">
            <span className="ws-pulse" aria-hidden />
            Updating
          </span>
        ) : null}
      </header>

      {progress.map((c, i) => (
        <div key={`progress-${i}`}>{renderRegisteredComponent(c, onResolve, resolvingId)}</div>
      ))}

      {kpis.length > 0 ? (
        <div className={`ws-kpi-grid cols-${primaryCols}`}>
          {kpis.map((c, i) => (
            <div key={`kpi-${i}`}>{renderRegisteredComponent(c, onResolve, resolvingId)}</div>
          ))}
        </div>
      ) : null}

      <div className={`ws-body layout-${layout}`}>
        {body.map((c, i) => (
          <div
            key={`${c.type}-${i}`}
            className={`ws-slot ws-slot-${c.type}`}
            data-component={c.type}
          >
            {renderRegisteredComponent(c, onResolve, resolvingId)}
          </div>
        ))}
        {!hasExceptionTable && pendingFromState.length > 0 ? (
          <div className="ws-slot ws-slot-exception_table" data-component="exception_table">
            {renderRegisteredComponent(
              {
                type: 'exception_table',
                data: {
                  title: 'Needs Your Attention',
                  rows: pendingFromState,
                },
              },
              onResolve,
              resolvingId,
            )}
          </div>
        ) : null}
      </div>
    </div>
  )
}
