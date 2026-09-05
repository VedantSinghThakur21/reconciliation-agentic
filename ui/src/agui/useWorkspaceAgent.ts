import { useCallback, useRef, useState } from 'react'
import { HttpAgent } from '@ag-ui/client'
import type { UIComponent, WorkspaceUI } from './schema'

type ProgressStep = { name: string; status: 'started' | 'finished' }

function extractWorkspace(state: unknown): WorkspaceUI | null {
  if (!state || typeof state !== 'object') return null
  const s = state as Record<string, unknown>
  const ws = s.workspace
  if (ws && typeof ws === 'object') return ws as WorkspaceUI
  return null
}

function mapDecisionLabel(decision: string): string {
  const d = decision.toUpperCase()
  if (d === 'APPROVE') return 'APPROVED'
  if (d === 'REJECT') return 'REJECTED'
  if (d === 'RESOLVE') return 'RESOLVED'
  if (d === 'ESCALATE') return 'ESCALATED'
  if (d === 'WRITE_OFF' || d === 'WRITEOFF') return 'WRITE_OFF'
  return d
}

function applyDecisionToComponents(
  components: UIComponent[],
  exceptionId: string,
  decision: string,
): UIComponent[] {
  const label = mapDecisionLabel(decision)
  return components.map((c) => {
    if (c.type === 'exception_table') {
      const rows = Array.isArray(c.data?.rows) ? [...(c.data.rows as Array<Record<string, unknown>>)] : []
      return {
        ...c,
        data: {
          ...c.data,
          rows: rows.map((r) =>
            String(r.id) === exceptionId
              ? { ...r, human_decision: label, status: 'decided' }
              : r,
          ),
        },
      }
    }
    if (c.type === 'recommendation' && String(c.data?.exception_id || '') === exceptionId) {
      return {
        ...c,
        data: { ...c.data, human_decision: label },
      }
    }
    return c
  })
}

export type WorkspaceRunOptions = {
  bank_pdf_path?: string | null
}

export function useWorkspaceAgent() {
  const agentRef = useRef<HttpAgent | null>(null)
  const [workspace, setWorkspace] = useState<WorkspaceUI | null>(null)
  const [assistantText, setAssistantText] = useState('')
  const [progress, setProgress] = useState<ProgressStep[]>([])
  const [running, setRunning] = useState(false)
  const [connectionError, setConnectionError] = useState<string | null>(null)
  const [agentError, setAgentError] = useState<string | null>(null)

  const ensureAgent = useCallback(() => {
    if (!agentRef.current) {
      agentRef.current = new HttpAgent({
        url: '/api/agui',
        agentId: 'reconq-workspace',
        description: 'ReconQ intent-driven finance workspace',
        initialState: {},
      })
    }
    return agentRef.current
  }, [])

  const run = useCallback(
    async (text: string, opts?: WorkspaceRunOptions): Promise<WorkspaceUI | null> => {
      const q = text.trim()
      if (!q) return null
      setRunning(true)
      setConnectionError(null)
      setAgentError(null)
      setAssistantText('')
      setProgress([])

      const agent = ensureAgent()
      const bankPdf = opts?.bank_pdf_path || undefined
      let latestWs: WorkspaceUI | null = null
      try {
        await agent.runAgent(
          {
            forwardedProps: bankPdf ? { bank_pdf_path: bankPdf } : {},
          },
          {
            onRunStartedEvent: () => {
              setConnectionError(null)
            },
            onStepStartedEvent: ({ event }) => {
              const name = event.stepName
              setProgress((prev) => {
                if (prev.some((p) => p.name === name && p.status === 'started')) return prev
                return [...prev, { name, status: 'started' }]
              })
            },
            onStepFinishedEvent: ({ event }) => {
              const name = event.stepName
              setProgress((prev) => {
                const without = prev.filter((p) => !(p.name === name && p.status === 'started'))
                return [...without, { name, status: 'finished' }]
              })
            },
            onStateSnapshotEvent: ({ event }) => {
              const ws = extractWorkspace(event.snapshot)
              if (ws) {
                latestWs = ws
                setWorkspace(ws)
              }
            },
            onStateChanged: ({ state }) => {
              const ws = extractWorkspace(state)
              if (ws) {
                latestWs = ws
                setWorkspace(ws)
              }
            },
            onTextMessageContentEvent: ({ event }) => {
              if (event.delta) setAssistantText((t) => t + event.delta)
            },
            onRunErrorEvent: ({ event }) => {
              setAgentError(event.message || 'Agent run error')
            },
            onRunFailed: ({ error }) => {
              const msg = error?.message || String(error)
              if (/failed to fetch|network|connection/i.test(msg)) {
                setConnectionError(msg)
              } else {
                setAgentError(msg)
              }
            },
            onRunFinalized: () => {
              /* no-op */
            },
          },
        )
      } catch (e) {
        const msg = String(e)
        if (/failed to fetch|network|NetworkError|ECONNREFUSED/i.test(msg)) {
          setConnectionError(msg)
        } else {
          setAgentError(msg)
        }
      } finally {
        setRunning(false)
      }
      return latestWs
    },
    [ensureAgent],
  )

  const runWithMessage = useCallback(
    async (text: string, opts?: WorkspaceRunOptions): Promise<WorkspaceUI | null> => {
      const agent = ensureAgent()
      agent.setMessages([
        {
          id: `user_${Date.now()}`,
          role: 'user',
          content: text,
        },
      ])
      return run(text, opts)
    },
    [ensureAgent, run],
  )

  /** Update AG-UI exception rows after a human decision — never re-runs CrewAI. */
  const applyExceptionDecision = useCallback((exceptionId: string, decision: string) => {
    setWorkspace((prev) => {
      if (!prev) return prev
      return {
        ...prev,
        components: applyDecisionToComponents(prev.components || [], exceptionId, decision),
      }
    })
  }, [])

  return {
    workspace,
    setWorkspace,
    assistantText,
    progress,
    running,
    connectionError,
    agentError,
    run: runWithMessage,
    applyExceptionDecision,
  }
}
