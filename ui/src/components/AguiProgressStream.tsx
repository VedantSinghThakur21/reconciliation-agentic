import { useEffect, useRef, useState } from 'react'

type Props = {
  kickoffId: string | null | undefined
  active: boolean
  /** Fallback copy when stream is quiet / fails (existing Thinking... path). */
  fallback?: string
}

/**
 * Minimal AG-UI EventSource consumer — reconcile + CrewAI AMP only.
 * Fail-open: on error/disconnect, show fallback and never throw.
 */
export default function AguiProgressStream({ kickoffId, active, fallback = 'Thinking...' }: Props) {
  const [text, setText] = useState(fallback)
  const [live, setLive] = useState(false)
  const esRef = useRef<EventSource | null>(null)

  useEffect(() => {
    if (!active || !kickoffId) {
      setText(fallback)
      setLive(false)
      return
    }

    let closed = false
    const chunks: string[] = []
    setText('Connecting to CrewAI stream…')
    setLive(false)

    try {
      const es = new EventSource(`/api/agui/stream/${encodeURIComponent(kickoffId)}`)
      esRef.current = es

      es.onmessage = (ev) => {
        if (closed) return
        try {
          const data = JSON.parse(ev.data) as {
            type?: string
            delta?: string
            stepName?: string
            message?: string
          }
          setLive(true)
          if (data.type === 'TEXT_MESSAGE_CONTENT' && data.delta) {
            chunks.push(data.delta)
            // Keep last ~4 lines for the thinking panel
            const lines = chunks.join('').trim().split(/\n+/).filter(Boolean)
            setText(lines.slice(-4).join('\n') || fallback)
          } else if (data.type === 'STEP_STARTED' && data.stepName) {
            setText((prev) => prev || `Step: ${data.stepName}`)
          } else if (data.type === 'RUN_ERROR') {
            setText(fallback)
            setLive(false)
            es.close()
          } else if (data.type === 'RUN_FINISHED') {
            setText((prev) => prev || 'Enrichment complete.')
            es.close()
          }
        } catch {
          // ignore malformed frames
        }
      }

      es.onerror = () => {
        // Silent fallback — existing flow continues via ampStatus poll
        setLive(false)
        setText((prev) => prev || fallback)
        es.close()
      }
    } catch {
      setText(fallback)
      setLive(false)
    }

    return () => {
      closed = true
      esRef.current?.close()
      esRef.current = null
    }
  }, [active, kickoffId, fallback])

  return (
    <div className="dash-query-thinking" role="status" data-agui={live ? 'live' : 'fallback'}>
      {live ? <span className="mono" style={{ opacity: 0.7 }}>AG-UI · </span> : null}
      <span style={{ whiteSpace: 'pre-wrap' }}>{text || fallback}</span>
    </div>
  )
}
