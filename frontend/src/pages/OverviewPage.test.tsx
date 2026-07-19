import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { OverviewPage } from './OverviewPage'

vi.mock('../components/TrafficChart', () => ({ TrafficChart: () => null }))

afterEach(() => vi.restoreAllMocks())

describe('OverviewPage', () => {
  it('показывает только собранные статусы сервисов и выбранный резерв', async () => {
    vi.spyOn(api, 'overview').mockResolvedValue({
      client_count: 0,
      clients: [],
      mihomo_version: null,
      traffic: null,
      services: [{ name: 'mihomo', observed_at: '2026-07-13T12:00:00Z', succeeded: false, latency_ms: null, status: 'unavailable', status_code: 503 }],
      fallback: { primary: 'WG-IMP', reserve: 'HY2-NL', selected: 'HY2-NL' },
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
})
