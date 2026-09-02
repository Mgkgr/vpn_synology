import type {
  AuthSession,
  Client,
  DashboardAdmin,
  DirectRulesResult,
  JournalFilters,
  JournalResponse,
  Overview,
  Period,
  RealtimeTrafficPeriod,
  RealtimeTrafficResponse,
  RoutesResponse,
  RulesResponse,
  TrafficUsageResponse,
  UpdatesResponse,
  WgEasyCredentialStatus,
  ProbeTarget,
  ProbeTargetEditInput,
  ProbeTargetInput,
  ManagedRuleInput,
  ManagedRulePolicy,
  ProbeDiagnosis,
  HostHealth,
  GeoUpdate,
} from './types'

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message)
    this.name = 'ApiError'
  }
}

let csrfToken: string | null = null
let unauthorizedHandler: (() => void) | null = null

export function setUnauthorizedHandler(handler: () => void): () => void {
  unauthorizedHandler = handler
  return () => {
    if (unauthorizedHandler === handler) unauthorizedHandler = null
  }
}

async function request<T>(path: string, init: RequestInit = {}, notifyUnauthorized = true): Promise<T> {
  const headers = new Headers(init.headers)
  const mutates = !['GET', 'HEAD'].includes((init.method ?? 'GET').toUpperCase())
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (mutates && csrfToken) headers.set('X-CSRF-Token', csrfToken)

  const response = await fetch(`/api${path}`, { ...init, credentials: 'same-origin', headers })
  if (!response.ok) {
    if (response.status === 401 && notifyUnauthorized) {
      csrfToken = null
      unauthorizedHandler?.()
    }
    const message = response.status === 401
      ? 'Требуется вход в панель.'
      : response.status === 403
        ? 'Сессия истекла. Войдите снова.'
        : response.status === 503 && path === '/host-health'
          ? 'Снимок состояния NAS ещё не собран. Запустите задачу DSM «VPN Dashboard — host health». '
            + 'После первого запуска данные появятся в течение минуты.'
        : 'Операция не выполнена. Попробуйте ещё раз.'
    throw new ApiError(response.status, message)
  }
  if (response.status === 204 || response.status === 202) return undefined as T
  return response.json() as Promise<T>
}

async function openSession(path: '/auth/login', username: string, password: string): Promise<void> {
  const session = await request<AuthSession>(path, {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
  csrfToken = session.csrf_token
}

export const api = {
  async restoreSession(): Promise<AuthSession> {
    const session = await request<AuthSession>('/auth/csrf', {}, false)
    csrfToken = session.csrf_token
    return session
  },
  login(username: string, password: string) {
    return openSession('/auth/login', username, password)
  },
  administrators: () => request<DashboardAdmin[]>('/auth/admins'),
  createAdministrator: (username: string, password: string) => request<DashboardAdmin>('/auth/admins', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  }),
  revokeAdministratorSessions: (username: string) => request<void>('/auth/sessions/revoke', {
    method: 'POST',
    body: JSON.stringify({ username }),
  }),
  async logout(): Promise<void> {
    await request<void>('/auth/logout', { method: 'POST' })
    csrfToken = null
  },
  overview: () => request<Overview>('/overview'),
  hostHealth: () => request<HostHealth>('/host-health'),
  routes: () => request<RoutesResponse>('/routes'),
  clients: () => request<Client[]>('/clients'),
  wgeasyCredentialStatus: () => request<WgEasyCredentialStatus>('/wgeasy/credentials/status'),
  configureWgEasy: (username: string, password: string) => request<WgEasyCredentialStatus>('/wgeasy/credentials', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  }),
  createClient: (name: string) => request<Client>('/clients', { method: 'POST', body: JSON.stringify({ name }) }),
  disableClient: (clientId: number) => request<void>(`/clients/${clientId}/disable`, { method: 'POST' }),
  enableClient: (clientId: number) => request<void>(`/clients/${clientId}/enable`, { method: 'POST' }),
  renameClient: (clientId: number, name: string) => request<Client>(`/clients/${clientId}/rename`, {
    method: 'POST',
    body: JSON.stringify({ name }),
  }),
  deleteClient: (clientId: number) => request<void>(`/clients/${clientId}`, { method: 'DELETE' }),
  rules: () => request<RulesResponse>('/rules'),
  applyDirectRules: (text: string) => request<DirectRulesResult>('/rules/apply', { method: 'POST', body: JSON.stringify({ text }) }),
  createManagedRule: (payload: ManagedRuleInput) => request<ManagedRulePolicy>('/rules/policies', { method: 'POST', body: JSON.stringify(payload) }),
  updateManagedRule: (id: number, payload: ManagedRuleInput) => request<ManagedRulePolicy>(`/rules/policies/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteManagedRule: (id: number) => request<void>(`/rules/policies/${id}`, { method: 'DELETE' }),
  updates: () => request<UpdatesResponse>('/updates'),
  updateGeo: () => request<GeoUpdate>('/updates/geo', { method: 'POST' }),
  probeTargets: () => request<ProbeTarget[]>('/probes/targets'),
  updateProbeTargets: (targets: Pick<ProbeTarget, 'key' | 'enabled'>[]) => request<ProbeTarget[]>('/probes/targets', {
    method: 'PUT',
    body: JSON.stringify({ targets }),
  }),
  createProbeTarget: (payload: ProbeTargetInput) => request<ProbeTarget>('/probes/targets', {
    method: 'POST',
    body: JSON.stringify(payload),
  }),
  updateProbeTarget: (key: string, payload: ProbeTargetEditInput) => request<ProbeTarget>(`/probes/targets/${encodeURIComponent(key)}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  }),
  deleteProbeTarget: (key: string) => request<void>(`/probes/targets/${encodeURIComponent(key)}`, { method: 'DELETE' }),
  runProbes: () => request<void>('/probes/run', { method: 'POST' }),
  diagnoseProbe: (payload: { targetKey: string, outbound: 'WG-IMP' | 'HY2-USA' }) => request<ProbeDiagnosis>('/probes/diagnose', {
    method: 'POST',
    body: JSON.stringify({ target_key: payload.targetKey, outbound: payload.outbound }),
  }),
  trafficUsage: (period: Period) => request<TrafficUsageResponse>(`/traffic?period=${period}`),
  realtimeTraffic: (period: RealtimeTrafficPeriod) => request<RealtimeTrafficResponse>(`/traffic/realtime?period=${period}`),
  journal: (filters: JournalFilters = {}) => {
    const params = new URLSearchParams()
    for (const [name, value] of Object.entries(filters)) {
      if (value?.trim()) params.set(name, value.trim())
    }
    const query = params.toString()
    return request<JournalResponse>(`/journal${query ? `?${query}` : ''}`)
  },
  configUrl: (clientId: number) => `/api/clients/${clientId}/config`,
  qrUrl: (clientId: number) => `/api/clients/${clientId}/qr`,
  clearSession() {
    csrfToken = null
  },
}
