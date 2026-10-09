import type { StrategyProgress } from './antidpiTypes'
import type { ProfileProgress } from './profileTypes'

export type ComponentId = 'wireguard' | 'mihomo' | 'uptime-kuma' | 'metacubexd' | 'dashboard' | 'antidpi'
export type MaintenanceOperation = {
  action: 'restart' | 'update' | 'rollback'
  components: ComponentId[]
  expected_revision: string
  release_ids: Partial<Record<ComponentId, string>>
  enable_stopped: ComponentId[]
  snapshot_id: string | null
  accept_data_loss: boolean
}
export type CancelOperation = { action: 'cancel'; job_id: string }
export type MaintenanceJob = {
  job_id: string; phase: string; component?: ComponentId | null; actor?: string
  created_at?: number; started_at?: number | null; finished_at?: number | null
  maintenance_until?: number | null; error_code?: string | null
  cancel_allowed: boolean; cancel_requested?: boolean
  strategy?: StrategyProgress
  profile?: ProfileProgress
}
export type ComponentRelease = {
  release_id: string; version: string; published_at: string; digest: string | null
  compatibility: string; release_notes_url: string | null
}
export type ComponentInventory = {
  component: ComponentId; installed: boolean; running: boolean
  actual_version: string | null; actual_digest: string | null; expected_digest: string | null
  checked_at: string | null; freshness_error: string | null
  artifacts: { service: string; actual_digest: string | null; expected_digest: string | null }[]
  release: { releases: ComponentRelease[]; checked_at: string | null; freshness_error: string | null } | null
}
export type MaintenanceInventory = {
  available: boolean; can_maintain: boolean; can_check?: boolean; revision: string | null
  components: ComponentInventory[]
  backup_snapshots?: { snapshot_id: string; components: ComponentId[]; verified_at: number; captured_revision: string }[]
}
