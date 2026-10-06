import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import type { ServiceProbe } from '../../api/types'
import { ServiceProbeStatus, ServiceProbeSummary } from './ServiceProbeStatus'

const observed = '2026-10-04T00:02:00Z'
const service: ServiceProbe = { key: 'openai', category: 'openai', url: 'https://chatgpt.com/', routes: [
  { route_id: 'direct', label: 'DIRECT', state: 'responded', delay_ms: 127, reason: null, observed_at: observed, url: 'https://chatgpt.com/', stale: false, current_run: true },
  { route_id: 'primary', label: 'Основной · прежнее имя', state: 'http_rejected', delay_ms: 203, reason: 'http_status_outside_expected', observed_at: observed, url: 'https://chatgpt.com/', stale: false, current_run: true },
  { route_id: 'reserve', label: 'Резервный · HY2-DE', state: 'failed', delay_ms: null, reason: 'probe_timeout', observed_at: observed, url: 'https://chatgpt.com/', stale: false, current_run: true },
] }
afterEach(cleanup)

describe('daily service probe display', () => {
  it('shows measured delays, original labels, time and honest HEAD limitations in expanded details', () => {
    const { container } = render(<ServiceProbeStatus service={service} enabled expanded />)
    expect(screen.getByText('DIRECT · 127 мс')).toBeVisible()
    expect(screen.getByText('Основной · HTTP-ответ · 203 мс')).toBeVisible()
    expect(screen.getByText('Резерв · таймаут')).toBeVisible()
    expect(screen.getByText('Основной · прежнее имя')).toBeVisible()
    expect(screen.getByText(/Вне 2xx\/3xx; точный код API не сообщает/)).toBeVisible()
    expect(screen.getByText(/HEAD не проверяет вход, видео или работу всего приложения/)).toBeVisible()
    expect(container.querySelector('time')).toHaveAttribute('dateTime', observed)
    expect(screen.getAllByText('https://chatgpt.com/').length).toBeGreaterThan(0)
    expect(screen.queryByText('VPN не работает')).not.toBeInTheDocument()
  })

  it.each(['stale', 'previous', 'disabled', 'error'] as const)('never paints an old or unconfirmed response green: %s', (mode) => {
    const old: ServiceProbe = { ...service, routes: service.routes.map((route) => ({ ...route, stale: mode === 'stale', current_run: mode !== 'previous' })) }
    const { container } = render(<ServiceProbeStatus service={old} enabled={mode !== 'disabled'} error={mode === 'error'} expanded />)
    expect(container.querySelector('[data-probe-tone="good"]')).toBeNull()
    expect(screen.getByText(mode === 'stale' ? 'DIRECT · устарело' : mode === 'previous' ? 'DIRECT · прошлый прогон' : mode === 'disabled' ? 'DIRECT · архив' : 'DIRECT · не подтверждено')).toBeVisible()
  })

  it('does not invent a result for a broad category or for a check that never ran', () => {
    const { rerender } = render(<ServiceProbeStatus enabled />)
    expect(screen.getByText('Для категории нет единого контрольного сайта')).toBeVisible()
    rerender(<ServiceProbeStatus enabled service={{ ...service, routes: [{ ...service.routes[0], state: 'unknown', delay_ms: null, observed_at: null, reason: 'not_checked', current_run: false }] }} />)
    expect(screen.getByText('DIRECT · ещё не проверено')).toBeVisible()
    expect(screen.queryByText('0 мс')).not.toBeInTheDocument()
  })

  it('shows a partial run and fixed schedule, or that collection is disabled', () => {
    const data = { enabled: true, timezone: 'Asia/Yekaterinburg', next_run_at: '2026-10-04T23:30:00Z', run: { day: '2026-10-04', state: 'interrupted' as const, started_at: observed, completed_at: observed, expected_count: 108, completed_count: 12 }, services: [service] }
    const { rerender } = render(<ServiceProbeSummary data={data} />)
    expect(screen.getByText(/04:30 · Asia\/Yekaterinburg/)).toBeVisible()
    expect(screen.getByText(/Прогон прерван: 12 из 108/)).toBeVisible()
    rerender(<ServiceProbeSummary data={{ ...data, enabled: false, next_run_at: null }} />)
    expect(screen.getByText(/Суточные замеры выключены/)).toBeVisible()
    expect(screen.queryByText(/Следующий прогон/)).not.toBeInTheDocument()
  })
})
