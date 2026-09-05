import { useRef, useState } from 'react'
import { useRun } from '../state/RunContext'

export default function ConnectionsPage() {
  const { sources, bankPdfPath, bankPdfMeta, uploadBankPdf, setBankPdf, setError, running } = useRun()
  const inputRef = useRef<HTMLInputElement>(null)
  const [uploading, setUploading] = useState(false)

  async function onPick(file: File | null) {
    if (!file) return
    setUploading(true)
    try {
      await uploadBankPdf(file)
    } catch (e) {
      setError(String(e))
    } finally {
      setUploading(false)
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>Connections</h1>
          <p>
            CrewAI AMP runs on CrewAI servers (not your disk). Uploading a bank PDF publishes a temporary
            public URL and sends it as <span className="mono">bank_pdf_path</span> so the extractor can fetch it.
          </p>
        </div>
      </div>

      <div className="card card-pad">
        <h2 className="card-title">Bank statement PDF</h2>
        <p className="card-sub">
          Upload → tmpfiles.org public URL → AMP kickoff <span className="mono">bank_pdf_path</span>. Local
          path is kept for preview only; AMP must receive an https URL.
        </p>
        <div className="row-actions" style={{ marginTop: 12, alignItems: 'center' }}>
          <input
            ref={inputRef}
            type="file"
            accept="application/pdf,.pdf"
            hidden
            onChange={(e) => onPick(e.target.files?.[0] || null)}
          />
          <button
            type="button"
            className="btn btn-primary"
            disabled={uploading || running}
            onClick={() => inputRef.current?.click()}
          >
            {uploading ? 'Uploading…' : 'Upload bank PDF'}
          </button>
          {bankPdfPath && (
            <button type="button" className="btn btn-secondary" disabled={running} onClick={() => setBankPdf(null)}>
              Clear PDF
            </button>
          )}
        </div>
        {bankPdfPath ? (
          <div className="source-card" style={{ marginTop: 14 }}>
            <strong>{bankPdfMeta?.filename || bankPdfPath}</strong>
            <div className="meta">
              bank_pdf_path=<span className="mono">{bankPdfPath}</span>
              {bankPdfMeta?.records != null ? ` · preview ${bankPdfMeta.records} credits` : ''}
            </div>
            <div className="status">{bankPdfMeta?.note || 'Attached for next reconciliation run'}</div>
          </div>
        ) : (
          <div className="empty" style={{ marginTop: 14 }}>
            <strong>No PDF attached</strong>
            Reconciliation will use demo bank CSV only until you upload a statement.
          </div>
        )}
      </div>

      <div className="grid-3">
        {sources.map((s) => (
          <div className="source-card" key={s.id}>
            <strong>{s.label}</strong>
            <div className="meta">
              {s.count} {s.kind} · {s.mode} mode
            </div>
            <div className="status">Connected</div>
          </div>
        ))}
      </div>

      <div className="card">
        <div className="card-pad" style={{ borderBottom: '1px solid var(--line)' }}>
          <h2 className="card-title">Flow inputs</h2>
          <p className="card-sub" style={{ marginBottom: 0 }}>Mapped to AIFinanceController / AMP kickoff fields</p>
        </div>
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Input</th>
                <th>Role</th>
                <th>Path</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>ERP Loader</td>
                <td>20+ stub Indian-company ERP txns</td>
                <td className="mono">generated in-flow</td>
              </tr>
              <tr>
                <td>bank_csv_path</td>
                <td>Bank transactions (CSV)</td>
                <td className="mono">data/demo/bank.csv</td>
              </tr>
              <tr>
                <td>bank_pdf_path</td>
                <td>Bank statement PDF (AMP OCR / local text)</td>
                <td className="mono">{bankPdfPath || '— (optional upload)'}</td>
              </tr>
              <tr>
                <td>payment_processor_csv_path</td>
                <td>Processor settlements</td>
                <td className="mono">data/demo/payment_processor.csv</td>
              </tr>
              <tr>
                <td>ground_truth_csv_path</td>
                <td>Evaluation labels</td>
                <td className="mono">data/demo/ground_truth.csv</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
