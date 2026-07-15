export type Period = 'month' | 'year'
export type RealtimeTrafficPeriod = '5m' | '30m' | '6h'

export interface AuthSession {
  csrf_token: string
}

export interface DashboardAdmin {
  username: string
  bootstrap_owner: boolean
}

export interface Client {
  id: number
  name: string
  enabled: boolean
  ipv4_address: string
  latest_handshake_at: string | null
  received_bytes: number
  transmitted_bytes: number
}

export interface WgEasyCredentialStatus {
  configured: boolean
}

export interface Overview {
  client_count: number
  clients: Client[]
  mihomo_version: string | null
  traffic: { up: number; down: number } | null
  services: ServiceStatus[]
  fallback: FallbackState
}

export interface ServiceStatus {
  name: string
  observed_at: string
  succeeded: boolean
  latency_ms: number | null
  status: string | null
  status_code: number | null
}

export interface FallbackState {
  primary: 'WG-IMP'
  reserve: 'HY2-NL'
  selected: 'WG-IMP' | 'HY2-NL' | null
}

export interface RouteGroup {
  name: string
  kind: string
  choices: string[]
  selected: string | null
}

export interface RoutesResponse {
  groups: RouteGroup[]
  fallback: FallbackState
  probes: RouteProbe[]
  last_switch: RouteSwitch | null
}

export interface RouteProbe {
  observed_at: string
  target: 'WG-IMP' | 'HY2-NL'
  succeeded: boolean
  latency_ms: number | null
  endpoint: string | null
  outbound: 'WG-IMP' | 'HY2-NL' | null
  status: string | null
  status_code: number | null
  reason: string | null
}

export interface RouteSwitch {
  observed_at: string
  route: string
  action: string
  previous_outbound: 'WG-IMP' | 'HY2-NL' | null
  new_outbound: 'WG-IMP' | 'HY2-NL' | null
}

export interface ProbeTarget {
  key: string
  label: string
  url: string
  enabled: boolean
  position: number
  is_custom: boolean
}

export interface ProbeTargetInput {
  label: string
  url: string
}

export interface ProbeTargetEditInput extends ProbeTargetInput {
  enabled: boolean
}

export interface ProbeDiagnosticStep {
  succeeded: boolean | null
  latency_ms: number | null
  reason: string | null
}

export interface ProbeDiagnosticDns extends ProbeDiagnosticStep {
  hostname: string
  addresses: string[]
}

export interface ProbeDiagnosis {
  observed_at: string
  outbound: 'WG-IMP' | 'HY2-NL'
  endpoint: string
  conclusion: 'ok' | 'controller_unavailable' | 'dns_failure' | 'dns_no_address' | 'exit_failure'
  conclusion_text: string
  controller: ProbeDiagnosticStep
  dns: ProbeDiagnosticDns
  exit: ProbeDiagnosticStep
}

export interface ControllerRule {
  type: string
  payload: string
  proxy: string
}

export interface RuleProvider {
  name: string
  behavior: string
}

export interface RulesResponse {
  rules: ControllerRule[]
  providers: RuleProvider[]
  direct_text: string
  policies?: ManagedRulePolicy[]
  policy_catalog?: ManagedRuleCategory[]
}

export type ManagedRuleAction = 'DIRECT' | 'VPS-FALLBACK' | 'WG-IMP' | 'HY2-NL'

export interface ManagedRuleCategory {
  kind: 'GEOSITE' | 'GEOIP'
  category: string
  label: string
  description?: string
}

export interface ManagedRulePolicy extends ManagedRuleCategory {
  id: number
  action: ManagedRuleAction
  enabled: boolean
}

export interface ManagedRuleInput {
  kind: 'GEOSITE' | 'GEOIP'
  category: string
  action: ManagedRuleAction
  enabled: boolean
}

export interface DirectRulesResult {
  revision_number: number
  sha256: string
}

export interface GeoUpdate {
  id: number
  observed_at: string
  operation: string | null
  succeeded: boolean | null
  status_code: number | null
  version: string | null
}

export interface GeoAsset {
  filename: string
  kind: string
  size_bytes: number
  modified_at: string
  sha256: string
  last_observed_at: string | null
}

export interface RuleChange {
  observed_at: string
  actor: string
  action: string
  subject: string
  succeeded: boolean | null
  revision_number: number | null
}

export interface UpdatesResponse {
  updates: GeoUpdate[]
  assets?: GeoAsset[]
  assets_error?: string | null
  rule_changes?: RuleChange[]
}

export interface JournalEvent {
  id: number
  kind: 'audit' | 'route' | 'probe'
  observed_at: string
  actor: string | null
  action: string
  outbound: string | null
  endpoint: string | null
  revision_number: number | null
  succeeded: boolean | null
  status_code: number | null
  restored: boolean | null
  latency_ms: number | null
}

export interface JournalResponse {
  events: JournalEvent[]
  page: number
  page_size: number
  has_more: boolean
}

export interface TrafficUsage {
  peer_id: string | null
  peer_name: string
  received_bytes: number
  transmitted_bytes: number
}

export interface TrafficUsageResponse {
  period: Period
  usage: TrafficUsage[]
}

export interface RealtimeTrafficPoint {
  observed_at: string
  up_bps: number
  down_bps: number
}

export interface RealtimeTrafficResponse {
  period: RealtimeTrafficPeriod
  sample_interval_seconds: number
  points: RealtimeTrafficPoint[]
}

export interface JournalFilters {
  from?: string
  to?: string
  action?: string
  outbound?: string
  endpoint?: string
  page?: string
  page_size?: string
}
