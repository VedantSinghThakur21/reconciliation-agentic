import { useState } from 'react'
import { useRun } from '../state/RunContext'

const CHIPS = [
  'What is the match rate?',
  'How many transactions are unresolved?',
  'What is the cash position?',
  'What is measured accuracy?',
  'What was throughput?',
]

export default function AssistantPage() {
  const { ask, audit, setError, runId } = useRun()
  const [question, setQuestion] = useState(CHIPS[1])
  const [answer, setAnswer] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(q?: string) {
    const next = q ?? question
    setQuestion(next)
    setBusy(true)
    try {
      const a = await ask(next)
      setAnswer(a)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>Assistant</h1>
          <p>Ask settlement questions about the active run. Answers are logged to the audit trail.</p>
        </div>
      </div>

      <div className="grid-2">
        <div className="card card-pad">
          <h2 className="card-title">Ask the finance controller</h2>
          <p className="card-sub">
            {runId ? `Context: ${runId}` : 'Run reconciliation first so answers have data.'}
          </p>
          <div className="chips">
            {CHIPS.map((c) => (
              <button key={c} className="chip" onClick={() => submit(c)} disabled={busy}>
                {c}
              </button>
            ))}
          </div>
          <div className="qa-box">
            <input
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && submit()}
              placeholder="Ask about match rate, cash, exceptions…"
            />
            <button className="btn btn-primary" onClick={() => submit()} disabled={busy}>
              {busy ? '…' : 'Ask'}
            </button>
          </div>
          {answer && <div className="qa-answer">{answer}</div>}
        </div>

        <div className="card card-pad">
          <h2 className="card-title">Audit log</h2>
          <p className="card-sub">Stages, resolutions, and assistant queries for this run</p>
          <ul className="audit-list">
            {audit.length === 0 && (
              <li>
                <div className="empty" style={{ padding: 12 }}>No audit events yet</div>
              </li>
            )}
            {audit.map((a) => (
              <li key={String(a.id)}>
                <span className="time">{String(a.created_at)}</span>
                {String(a.message)}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  )
}
