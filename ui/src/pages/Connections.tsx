import { useRun } from '../state/RunContext'

export default function ConnectionsPage() {
  const { sources } = useRun()

  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>Connections</h1>
          <p>CrewAI AMP deployment + local demo feeds. Live runs hit your deployed finance controller when `RECON_ENGINE=auto`.</p>
        </div>
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
          <p className="card-sub" style={{ marginBottom: 0 }}>Mapped to AIFinanceController kickoff fields</p>
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
                <td>Bank transactions</td>
                <td className="mono">data/demo/bank.csv</td>
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
