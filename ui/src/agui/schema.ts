/** Controlled UI schema — mirrors backend WorkspaceUI. Agent cannot invent types. */

export const ALLOWED_COMPONENT_TYPES = [
  'kpi',
  'metric_grid',
  'transaction_table',
  'exception_table',
  'chart',
  'transaction_detail',
  'comparison',
  'recommendation',
  'agent_progress',
  'audit_timeline',
  'clarification',
  'unavailable',
] as const

export type ComponentType = (typeof ALLOWED_COMPONENT_TYPES)[number]

export type IntentObject = {
  primary: string
  secondary?: string[]
}

export type UIComponent = {
  type: string
  data: Record<string, unknown>
}

export type WorkspaceUI = {
  intent: IntentObject | string
  title: string
  status: 'ok' | 'unavailable' | 'clarification' | 'error' | 'agent_failure'
  message?: string | null
  reasoning?: string | null
  components: UIComponent[]
  run_id?: string | null
  txn_ref?: string | null
  data?: Record<string, unknown>
  layout?: string | null
}

export function intentPrimary(intent: IntentObject | string | undefined): string {
  if (!intent) return ''
  if (typeof intent === 'string') return intent
  return intent.primary || ''
}

export function intentSecondary(intent: IntentObject | string | undefined): string[] {
  if (!intent || typeof intent === 'string') return []
  return intent.secondary || []
}

export function isAllowedComponentType(t: string): t is ComponentType {
  return (ALLOWED_COMPONENT_TYPES as readonly string[]).includes(t)
}

export const HUMAN_ACTIONS = ['APPROVE', 'REJECT', 'RESOLVE', 'ESCALATE', 'WRITE_OFF'] as const
export type HumanAction = (typeof HUMAN_ACTIONS)[number]
