import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import { RunProvider } from './state/RunContext'
import DashboardPage from './pages/Dashboard'
import InvoicesPage from './pages/Invoices'
import PaymentsPage from './pages/Payments'
import MatchesPage from './pages/Matches'
import ReviewsPage from './pages/Reviews'
import JournalsPage from './pages/Journals'
import CashPage from './pages/Cash'
import AssistantPage from './pages/Assistant'
import ConnectionsPage from './pages/Connections'

export default function App() {
  return (
    <BrowserRouter>
      <RunProvider>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<DashboardPage />} />
            <Route path="invoices" element={<InvoicesPage />} />
            <Route path="payments" element={<PaymentsPage />} />
            <Route path="matches" element={<MatchesPage />} />
            <Route path="reviews" element={<ReviewsPage />} />
            <Route path="journals" element={<JournalsPage />} />
            <Route path="cash" element={<CashPage />} />
            <Route path="assistant" element={<AssistantPage />} />
            <Route path="connections" element={<ConnectionsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </RunProvider>
    </BrowserRouter>
  )
}
