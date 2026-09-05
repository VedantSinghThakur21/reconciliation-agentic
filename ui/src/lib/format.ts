export function pct(n?: number | null) {
  if (n == null || Number.isNaN(n)) return '—'
  return `${(n * 100).toFixed(1)}%`
}

export function money(n?: number | null) {
  if (n == null || Number.isNaN(n)) return '—'
  return `₹${Number(n).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`
}

export const STAGE_ORDER = [
  'conversational_intake',
  'extract_bank_pdf',
  'ingest_and_validate',
  'normalize',
  'generate_candidates',
  'score_candidates',
  'route_decisions',
  'investigate_exceptions',
  'human_review',
  'persist_results',
  'create_journal_entries',
  'evaluate',
  'summary_report',
]
