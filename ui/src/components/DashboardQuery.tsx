import { useEffect, useState } from 'react'
import { DynamicWorkspaceView } from '../agui/registry'
import type { WorkspaceUI } from '../agui/schema'
import { useWorkspaceAgent } from '../agui/useWorkspaceAgent'
import { useRun } from '../state/RunContext'

/**
 * Frontend does not interpret natural language or fabricate reconciliation results.
 * Free-text is forwarded to the agent; rendering uses structured workspace state only.
 */
export default function DashboardQuery({
  onWorkspaceActive,
}: {
  onWorkspaceActive?: (active: boolean) => void
}) {
  const { resolve, loadLatest, selectRun, bankPdfPath } = useRun()
  const {
    workspace,
    setWorkspace,
    assistantText,
    progress,
    running,
    connectionError,
    agentError,
    run,
  } = useWorkspaceAgent()
  const [query, setQuery] = useState('')
  const [resolvingId, setResolvingId] = useState<string | null>(null)
  const [resolveError, setResolveError] = useState<string | null>(null)

  useEffect(() => {
    onWorkspaceActive?.(!!workspace && workspace.status === 'ok')
  }, [workspace, onWorkspaceActive])

  async function submit(raw?: string) {
    const text = (raw ?? query).trim()
    if (!text || running) return
    setQuery(text)
    setResolveError(null)
    const ws = await run(text, { bank_pdf_path: bankPdfPath })
    // Sync baseline dashboard KPIs to the run AMP just finalized
    const rid = ws?.run_id
    if (rid) {
      await selectRun(rid)
    } else {
      await loadLatest()
    }
  }

  async function onResolve(exceptionId: string, decision: string) {
    if (resolvingId || running) return
    setResolvingId(exceptionId)
    setResolveError(null)
    try {
      // Record human decision for this exception's run — do NOT re-ask the agent
      // (re-running "Reconcile today…" would start a brand-new CrewAI run and recreate pending rows).
      const res = await resolve(exceptionId, decision)
      const snap = res.workspace
      if (snap && typeof snap === 'object') {
        const next = (snap as { workspace?: WorkspaceUI }).workspace || (snap as WorkspaceUI)
        if (next && Array.isArray(next.components)) {
          setWorkspace(next)
        }
      }
      const rid = (res.run_id as string | undefined) || workspace?.run_id
      if (rid) {
        await selectRun(rid)
      } else {
        await loadLatest()
      }
    } catch (e) {
      setResolveError(String(e))
    } finally {
      setResolvingId(null)
    }
  }

  const latestStep = progress.length ? progress[progress.length - 1] : null

  return (
    <div className="ws-shell">
      <div className="card card-pad ws-command">
        <div className="ws-command-head">
          <div>
            <h2 className="card-title">Ask finance</h2>
            <p className="card-sub">
              Ask to run reconciliation — with AMP configured this kicks CrewAI Enterprise
              (visible under Executions). Approve / reject exceptions here without starting
              another run.
            </p>
          </div>
          {running ? (
            <span className="ws-live" aria-live="polite">
              <span className="ws-pulse" aria-hidden />
              {latestStep?.name || 'Working'}
            </span>
          ) : null}
        </div>

        <div className="qa-box">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && submit()}
            placeholder="e.g. Reconcile today’s batch · What needs attention? · Investigate BNK-…"
            disabled={running}
            aria-label="Finance workspace request"
          />
          <button
            className="btn btn-primary"
            type="button"
            onClick={() => submit()}
            disabled={running || !query.trim()}
          >
            {running ? 'Working…' : 'Ask'}
          </button>
        </div>

        {assistantText && !running ? (
          <p className="ws-assistant-text">{assistantText}</p>
        ) : null}
        {resolveError ? (
          <div className="ws-message ws-message-error" style={{ marginTop: 12 }} role="alert">
            <strong>Couldn’t record decision</strong>
            <p>{resolveError}</p>
          </div>
        ) : null}
      </div>

      <DynamicWorkspaceView
        workspace={workspace}
        onResolve={onResolve}
        connectionError={connectionError}
        agentError={agentError}
        running={running}
        resolvingId={resolvingId}
      />
    </div>
  )
}
