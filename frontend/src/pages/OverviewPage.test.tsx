import { cleanup, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { OverviewPage } from './OverviewPage'

vi.mock('../components/TrafficChart', () => ({ TrafficChart: () => null }))

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('OverviewPage', () => {
  it('shows only clients with a recent WireGuard handshake in the active list', async () => {
    const now = Date.now()
    vi.spyOn(api, 'overview').mockResolvedValue({
      client_count: 3,
      clients: [
        { id: 1, name: 'online-client', enabled: true, ipv4_address: '10.66.0.2', latest_handshake_at: new Date(now - 3 * 60_000).toISOString(), received_bytes: 10, transmitted_bytes: 20 },
        { id: 2, name: 'stale-client', enabled: true, ipv4_address: '10.66.0.3', latest_handshake_at: new Date(now - 6 * 60_000).toISOString(), received_bytes: 10, transmitted_bytes: 20 },
        { id: 3, name: 'never-connected', enabled: true, ipv4_address: '10.66.0.4', latest_handshake_at: null, received_bytes: 0, transmitted_bytes: 0 },
      ],
      mihomo_version: null,
      traffic: null,
      services: [],
      fallback: { primary: 'WG-IMP', reserve: 'HY2-USA', selected: 'WG-IMP' },
      exit_health: [],
    })
    vi.spyOn(api, 'realtimeTraffic').mockResolvedValue({ period: '30m', sample_interval_seconds: 60, points: [] })
    vi.spyOn(api, 'updates').mockResolvedValue({ updates: [] })
    vi.spyOn(api, 'journal').mockResolvedValue({ events: [], page: 1, page_size: 50, has_more: false })
    vi.spyOn(api, 'hostHealth').mockResolvedValue({
      observed_at: new Date(now).toISOString(), cpu_usage_percent: null, load_one: 0, load_five: 0, load_fifteen: 0,
      memory_total_bytes: 0, memory_available_bytes: 0, swap_total_bytes: 0, swap_free_bytes: 0,
      volume_total_bytes: 0, volume_available_bytes: 0, network_rx_errors: 0, network_rx_dropped: 0,
      network_tx_errors: 0, network_tx_dropped: 0, containers: [],
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={client}><OverviewPage /></QueryClientProvider>)

    expect(await screen.findByText('online-client')).toBeVisible()
    expect(screen.getByText('Активно: 1 из 3 · handshake ≤ 5 мин')).toBeVisible()
    expect(screen.getByText('подключён')).toBeVisible()
    expect(screen.queryByText('stale-client')).not.toBeInTheDocument()
    expect(screen.queryByText('never-connected')).not.toBeInTheDocument()
  })

  it('показывает только собранные статусы сервисов и выбранный резерв', async () => {
    vi.spyOn(api, 'overview').mockResolvedValue({
      client_count: 0,
      clients: [],
      mihomo_version: null,
      traffic: null,
      services: [{ name: 'mihomo', observed_at: '2026-07-13T12:00:00Z', succeeded: false, latency_ms: null, status: 'unavailable', status_code: 503 }],
      fallback: { primary: 'WG-IMP', reserve: 'HY2-USA', selected: 'HY2-USA' },
      exit_health: [],
    })
    vi.spyOn(api, 'realtimeTraffic').mockResolvedValue({ period: '30m', sample_interval_seconds: 60, points: [] })
    vi.spyOn(api, 'updates').mockResolvedValue({ updates: [] })
    vi.spyOn(api, 'journal').mockResolvedValue({ events: [], page: 1, page_size: 50, has_more: false })
    vi.spyOn(api, 'hostHealth').mockResolvedValue({
      observed_at: '2026-07-19T12:00:00Z',
      cpu_usage_percent: 12.5,
      load_one: 0.25,
      load_five: 0.2,
      load_fifteen: 0.15,
      memory_total_bytes: 1_000,
      memory_available_bytes: 750,
      swap_total_bytes: 500,
      swap_free_bytes: 500,
      volume_total_bytes: 10_000,
      volume_available_bytes: 3_500,
      network_rx_errors: 0,
      network_rx_dropped: 3,
      network_tx_errors: 1,
      network_tx_dropped: 0,
      containers: [{ name: 'vpn-dashboard', state: 'running', restart_count: 0, health: 'healthy' }],
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={client}><OverviewPage /></QueryClientProvider>)

    expect(await screen.findByText('mihomo')).toBeVisible()
    expect(screen.getByRole('button', { name: '30 минут' })).toHaveClass('active')
    expect(screen.getByText('переключён на резерв')).toBeVisible()
    expect(await screen.findByText('Хост DS923+')).toBeVisible()
    expect(screen.getByText('vpn-dashboard')).toBeVisible()
    expect(screen.queryByText('MetaCubeXD')).not.toBeInTheDocument()
  })

  it('показывает последнюю проверку резерва, а не статус его выбора fallback-группой', async () => {
    vi.spyOn(api, 'overview').mockResolvedValue({
      client_count: 0,
      clients: [],
      mihomo_version: null,
      traffic: null,
      services: [],
      fallback: { primary: 'WG-IMP', reserve: 'HY2-USA', selected: 'WG-IMP' },
      exit_health: [
        { name: 'WG-IMP', observed_at: '2026-09-02T07:00:00Z', succeeded: true, succeeded_count: 5, total_count: 5, last_success_at: '2026-09-02T07:00:00Z', unavailable_since: null },
        { name: 'HY2-USA', observed_at: '2026-09-02T07:00:00Z', succeeded: false, succeeded_count: 0, total_count: 5, last_success_at: '2026-09-02T06:55:00Z', unavailable_since: '2026-09-02T07:00:00Z' },
      ],
    })
    vi.spyOn(api, 'realtimeTraffic').mockResolvedValue({ period: '30m', sample_interval_seconds: 60, points: [] })
    vi.spyOn(api, 'updates').mockResolvedValue({ updates: [] })
    vi.spyOn(api, 'journal').mockResolvedValue({ events: [], page: 1, page_size: 50, has_more: false })
    vi.spyOn(api, 'hostHealth').mockResolvedValue({
      observed_at: '2026-09-02T07:00:00Z', cpu_usage_percent: null, load_one: 0, load_five: 0, load_fifteen: 0,
      memory_total_bytes: 0, memory_available_bytes: 0, swap_total_bytes: 0, swap_free_bytes: 0,
      volume_total_bytes: 0, volume_available_bytes: 0, network_rx_errors: 0, network_rx_dropped: 0,
      network_tx_errors: 0, network_tx_dropped: 0, containers: [],
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={client}><OverviewPage /></QueryClientProvider>)

    const title = await screen.findByRole('heading', { name: 'Резервный: HY2-USA' })
    const reserve = title.closest('article')
    expect(reserve).not.toBeNull()
    expect(within(reserve!).getByText('недоступен')).toBeVisible()
    expect(within(reserve!).getByText('не выбран fallback-группой.')).toBeVisible()
    expect(within(reserve!).getByText(/^Проверка: 0 из 5/)).toBeVisible()
  })

  it('показывает, как давно выход недоступен после последней успешной проверки', async () => {
    const now = Date.now()
    vi.spyOn(api, 'overview').mockResolvedValue({
      client_count: 0,
      clients: [],
      mihomo_version: null,
      traffic: null,
      services: [],
      fallback: { primary: 'WG-IMP', reserve: 'HY2-USA', selected: 'WG-IMP' },
      exit_health: [
        { name: 'WG-IMP', observed_at: new Date(now - 60_000).toISOString(), succeeded: true, succeeded_count: 5, total_count: 5, last_success_at: new Date(now - 60_000).toISOString(), unavailable_since: null },
        { name: 'HY2-USA', observed_at: new Date(now - 60_000).toISOString(), succeeded: false, succeeded_count: 0, total_count: 5, last_success_at: new Date(now - 20 * 60_000).toISOString(), unavailable_since: new Date(now - 10 * 60_000).toISOString() },
      ],
    })
    vi.spyOn(api, 'realtimeTraffic').mockResolvedValue({ period: '30m', sample_interval_seconds: 60, points: [] })
    vi.spyOn(api, 'updates').mockResolvedValue({ updates: [] })
    vi.spyOn(api, 'journal').mockResolvedValue({ events: [], page: 1, page_size: 50, has_more: false })
    vi.spyOn(api, 'hostHealth').mockResolvedValue({
      observed_at: new Date(now).toISOString(), cpu_usage_percent: null, load_one: 0, load_five: 0, load_fifteen: 0,
      memory_total_bytes: 0, memory_available_bytes: 0, swap_total_bytes: 0, swap_free_bytes: 0,
      volume_total_bytes: 0, volume_available_bytes: 0, network_rx_errors: 0, network_rx_dropped: 0,
      network_tx_errors: 0, network_tx_dropped: 0, containers: [],
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={client}><OverviewPage /></QueryClientProvider>)

    const title = await screen.findByRole('heading', { name: 'Резервный: HY2-USA' })
    const reserve = title.closest('article')
    expect(reserve).not.toBeNull()
    expect(within(reserve!).getByText(/Недоступен уже 10 мин/)).toBeVisible()
  })
})
