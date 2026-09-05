import type { Intent } from './types'
import { api } from '../api'

export type ParsedQuery = {
  intent: Intent
  txn_ref: string | null
  source?: string
  llm_intent?: string
}

/** LLM (or paraphrase heuristic) classifier — replaces chip substring matching. */
export async function classifyIntent(raw: string): Promise<ParsedQuery> {
  const text = raw.trim()
  if (!text) return { intent: 'unknown', txn_ref: null, source: 'empty' }
  const res = await api.classify(text)
  return {
    intent: res.intent as Intent,
    txn_ref: res.txn_ref,
    source: res.source,
    llm_intent: res.llm_intent,
  }
}

export function thinkingCopy(_intent?: Intent): string {
  return 'Thinking...'
}
