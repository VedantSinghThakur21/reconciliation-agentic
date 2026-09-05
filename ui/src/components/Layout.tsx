import { NavLink, Outlet } from 'react-router-dom'
import { useRun } from '../state/RunContext'

const NAV_MAIN = [
  { to: '/', label: 'Dashboard', end: true, ico: '◈' },
  { to: '/invoices', label: 'Invoices', ico: '▤' },
  { to: '/payments', label: 'Payments', ico: '⇄' },
  { to: '/matches', label: 'Matches', ico: '✓' },
  { to: '/reviews', label: 'Reviews', ico: '◎' },
  { to: '/journals', label: 'Journals', ico: '≣' },
]

const NAV_INSIGHTS = [
  { to: '/cash', label: 'Cash position', ico: '₹' },
  { to: '/assistant', label: 'Assistant', ico: '?' },
  { to: '/connections', label: 'Connections', ico: '⌁' },
]

export default function Layout() {
  const { error, running, runId, runRecon, loadLatest, useAi, setUseAi, exceptions } = useRun()
  const pending = exceptions.filter((e) => e.status === 'pending').length

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <div className="sidebar-logo">RQ</div>
          <div>
            <strong>ReconQ</strong>
            <span>Finance Controller</span>
          </div>
        </div>

        <div className="nav-section">
          <div className="nav-label">Workspace</div>
          {NAV_MAIN.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
            >
              <span className="ico">{item.ico}</span>
              {item.label}
              {item.to === '/reviews' && pending > 0 ? ` (${pending})` : ''}
            </NavLink>
          ))}
        </div>

        <div className="nav-section">
          <div className="nav-label">Insights</div>
          {NAV_INSIGHTS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
            >
              <span className="ico">{item.ico}</span>
              {item.label}
            </NavLink>
          ))}
        </div>

        <div className="sidebar-footer">
          Demo workspace · AMP + local flow
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="topbar-left">
            {runId ? (
              <>
                <span>Active run</span>
                <span className="run-id">{runId}</span>
              </>
            ) : (
              <span>No active run — start reconciliation to load data</span>
            )}
          </div>
          <div className="topbar-actions">
            <label className="toggle">
              <input
                type="checkbox"
                checked={useAi}
                onChange={(e) => setUseAi(e.target.checked)}
                disabled={running}
              />
              AI matching
            </label>
            <button className="btn btn-secondary btn-sm" onClick={loadLatest} disabled={running}>
              Refresh
            </button>
            <button className="btn btn-primary btn-sm" onClick={runRecon} disabled={running}>
              {running ? 'Running…' : 'Run reconciliation'}
            </button>
          </div>
        </header>

        {error && <div className="error-banner">{error}</div>}

        <div className="content">
          <Outlet />
        </div>
      </div>
    </div>
  )
}
