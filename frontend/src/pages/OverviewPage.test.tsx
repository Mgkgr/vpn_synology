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
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={client}><OverviewPage /></QueryClientProvider>)

    expect(await screen.findByText('mihomo')).toBeVisible()
    expect(screen.getByRole('button', { name: '30 минут' })).toHaveClass('active')
    expect(screen.getByText('переключён на резерв')).toBeVisible()
    expect(screen.queryByText('MetaCubeXD')).not.toBeInTheDocument()
  })
})
