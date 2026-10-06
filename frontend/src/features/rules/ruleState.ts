import { ApiError, api } from '../../api/client'
import { outboundLabel } from '../../api/outboundLabels'
import type { ManagedRuleAction, RulesResponse } from '../../api/types'

export const routeChoices: { value: ManagedRuleAction; label: string }[] = [
  { value: 'VPS-FALLBACK', label: 'Авто VPN' },
  { value: 'WG-IMP', label: outboundLabel('WG-IMP') },
  { value: 'HY2-USA', label: outboundLabel('HY2-USA') },
  { value: 'DIRECT', label: 'DIRECT' },
]

export function actionLabel(action: ManagedRuleAction): string {
  return routeChoices.find((item) => item.value === action)?.label ?? action
}

export function sameLines(left: string | null, right: string): boolean {
  if (left === null) return false
  const lines = (text: string) => text.split('\n').map((line) => line.trim()).filter(Boolean)
  return JSON.stringify(lines(left)) === JSON.stringify(lines(right))
}

/** GET-only reconciliation. Matching storage is not proof of a completed reload. */
export async function reconcileRules(error: unknown, matches: (rules: RulesResponse) => boolean): Promise<RulesResponse> {
  if (error instanceof ApiError && error.status < 500) throw error
  for (const delay of [0, 500, 1000]) {
    if (delay) await new Promise((resolve) => window.setTimeout(resolve, delay))
    try {
      const rules = await api.rules()
      if (matches(rules)) return rules
    } catch { /* A transient GET failure never triggers another write. */ }
  }
  throw new Error('Результат не подтверждён. Обновите данные перед повторным изменением; запрос мог сохраниться.')
}
