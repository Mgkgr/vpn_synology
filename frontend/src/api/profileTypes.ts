export type ProfileOperation = { action: 'profile_check' | 'profile_apply'; draft_id: string; expected_revision: string }
export type ProfileSummary = { target: 'WG-IMP' | 'HY2-USA'; protocol: 'vless' | 'hysteria2' | 'wireguard'; server: string; port: number }
export type ProfileProbe = { target: 'cloudflare' | 'google' | 'github'; ok: boolean; latency_ms: number | null; http_status: number | null; reason: string | null; checked_at: number }
export type ProfileDraft = ProfileSummary & { draft_id: string; revision: string; created_at: number; expires_at: number; checked_at: number | null; check_passed: boolean; endpoint_ip: string | null; results: ProfileProbe[] }
export type ProfileSnapshot = { available: boolean; can_manage: boolean; revision: string | null; current: ProfileSummary[]; drafts: ProfileDraft[] }
export type ProfileProgress = { action: ProfileOperation['action']; step: string; completed: number; limit: number; results: ProfileProbe[] }
