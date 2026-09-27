import { cleanup, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { OverviewPage } from './OverviewPage'
import { RoutesPage } from './RoutesPage'
import { routeFixture } from './RoutesPage'
import { JournalPage } from './JournalPage'

vi.mock('../components/TrafficChart', () => ({ TrafficChart: () => null }))
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('route selection is not reported as confirmed availability', () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const { container } = render(<QueryClientProvider client={client}><RoutesPage data={routeFixture({ active: 'HY2-USA' })} /></QueryClientProvider>)
  expect(within(container.querySelector('.page-header')!).queryByText('в норме')).not.toBeInTheDocument()
  expect(screen.queryByText('готов к работе')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: /ОСНОВНОЙ.*не выбран группой/ })).toBeVisible()
  expect(screen.getByRole('button', { name: /РЕЗЕРВ.*выбран группой/ })).toBeVisible()
})

it('overview does not infer health from the selected fallback', async () => {
  vi.spyOn(api, 'outboundHealth').mockRejectedValue(new Error('no health data'))
  vi.spyOn(api, 'overview').mockResolvedValue({ client_count: 0, clients: [], services: [], mihomo_version: null, traffic: null, exit_health: [], fallback: { primary: 'WG-IMP', reserve: 'HY2-USA', selected: 'HY2-USA' } })
  vi.spyOn(api, 'hostHealth').mockRejectedValue(new Error('host offline'))
  vi.spyOn(api, 'updates').mockResolvedValue({ updates: [] })
  vi.spyOn(api, 'journal').mockResolvedValue({ events: [], page: 1, page_size: 50, has_more: false })
  vi.spyOn(api, 'realtimeTraffic').mockResolvedValue({ period: '30m', sample_interval_seconds: 60, points: [] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const { container } = render(<QueryClientProvider client={client}><OverviewPage /></QueryClientProvider>)
  expect(await screen.findByText('выбран HY2-DE')).toBeVisible()
  expect(within(container.querySelector('.page-header')!).queryByText('в норме')).not.toBeInTheDocument()
})

it.each([OverviewPage, RoutesPage])('health survives failure of the adjacent page query', async (Page) => {
  vi.spyOn(api, 'outboundHealth').mockResolvedValue({
    enabled: true, observed_at: new Date().toISOString(), collector_state: 'unknown',
    outbounds: [{ id: 'WG-IMP', label: 'VLESS-NL', engine: 'vless', state: 'unknown', observed_at: null, pending_since: null, incident_id: null, incident_started_at: null, last_success_at: null, recovery_streak: 0, successes: 0, total: 3, reasons: ['no_data'], selected_fallback: null }],
    delivery: { state: 'disabled', error_code: null, telegram_delivery: 'unconfirmed', monitors: [] },
  })
  vi.spyOn(api, 'overview').mockRejectedValue(new Error('overview offline'))
  vi.spyOn(api, 'routes').mockRejectedValue(new Error('routes offline'))
  vi.spyOn(api, 'probeTargets').mockResolvedValue([])
  vi.spyOn(api, 'hostHealth').mockRejectedValue(new Error('host offline'))
  vi.spyOn(api, 'updates').mockResolvedValue({ updates: [] })
  vi.spyOn(api, 'journal').mockResolvedValue({ events: [], page: 1, page_size: 50, has_more: false })
  vi.spyOn(api, 'realtimeTraffic').mockResolvedValue({ period: '30m', sample_interval_seconds: 60, points: [] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><Page /></QueryClientProvider>)
  expect(await screen.findByRole('article', { name: 'VLESS-NL' })).toBeVisible()
  expect(screen.getByText(/Нет данных · последняя проверка/)).toBeVisible()
})

it('journal labels the tested exit separately from the chosen fallback', async () => {
  vi.spyOn(api, 'journal').mockResolvedValue({ events: [{ id: 1, kind: 'probe', observed_at: new Date().toISOString(), actor: null, action: 'probe', target: 'WG-IMP', outbound: 'HY2-USA', endpoint: null, revision_number: null, succeeded: false, status_code: null, restored: null, latency_ms: null }], page: 1, page_size: 50, has_more: false })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><JournalPage /></QueryClientProvider>)
  const rows = await screen.findAllByRole('row')
  expect(within(rows[0]).getByText('Проверяемый выход / сервис')).toBeVisible()
  expect(within(rows[0]).getByText('Выбранный fallback')).toBeVisible()
  expect(within(rows[1]).getByText('VLESS-NL')).toBeVisible()
  expect(within(rows[1]).getByText('HY2-DE')).toBeVisible()
})
