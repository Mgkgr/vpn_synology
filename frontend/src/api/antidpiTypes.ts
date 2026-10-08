export type AntidpiServiceId = 'youtube' | 'discord' | 'telegram' | 'instagram'
export type StrategyId = 'tlsrec-sni' | 'disorder-1' | 'disorder-sni' | 'split-1' | 'split-sni' | 'oob-sni' | 'disoob-sni' | 'fake-md5'
export type StrategyMode = 'auto' | 'pinned'
export type StrategyPolicy = { enabled: boolean; mode: StrategyMode; interval_minutes: 5 | 15 | 30 | 60; daily_enabled: boolean }
type OperationBase = { service_id: AntidpiServiceId; expected_revision: string }
export type StrategyOperation = OperationBase & (
  { action: 'strategy_check' | 'strategy_tune' | 'strategy_rollback'; settings: Record<string, never> }
  | { action: 'strategy_configure'; settings: StrategyPolicy }
  | { action: 'strategy_apply'; settings: { strategy_id: StrategyId; mode: StrategyMode } }
)
export type StrategyObservation = {
  checked_at: number; verdict: 'success' | 'transport_error' | 'http_error' | 'unknown' | 'certificate_error'
  reason: string; latency_ms: number | null; http_status: number | null
  infrastructure_ok: boolean; identity: string; context_id: string
}
export type StrategyResult = StrategyObservation & { strategy_id: StrategyId; source: string }
export type StrategyService = StrategyPolicy & {
  service_id: AntidpiServiceId; name: string; host: string; strategy_id: StrategyId | null; previous_strategy_id: StrategyId | null
  state: string; failure_count: number; first_failure_at: number | null; last_success_at: number | null
  last_search_at: number | null; last_check: StrategyResult | null; last_automatic: StrategyObservation | null
  results: StrategyResult[]
  history: { changed_at: number; previous: StrategyId | null; strategy: StrategyId; reason: string; actor: string }[]
}
export type StrategyProgress = { step: string; completed: number; limit: number; kind: string; results: StrategyResult[] }
export type StrategySnapshot = {
  available: boolean; can_manage: boolean; revision: string | null; identity: string | null; observed_at: number | null
  capabilities: { can_check: boolean; can_apply: boolean; can_configure: boolean; blockers: string[] }
  catalog: { version: string; catalog_id: string; strategies: StrategyId[]; intervals: (5 | 15 | 30 | 60)[]; daily_time: string; timezone: string } | null
  services: StrategyService[]
}
