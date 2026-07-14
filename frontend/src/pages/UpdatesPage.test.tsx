import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { UpdatesPage } from './UpdatesPage'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('UpdatesPage', () => {
  it('показывает фактическую дату файлов GeoData и историю изменений правил', async () => {
    vi.spyOn(api, 'updates').mockResolvedValue({
      assets: [
        { filename: 'geoip.dat', kind: 'GeoIP', size_bytes: 10, modified_at: '2026-07-14T10:00:00Z', sha256: 'a'.repeat(64), last_observed_at: '2026-07-14T10:00:00Z' },
        { filename: 'geosite.dat', kind: 'GeoSite', size_bytes: 12, modified_at: '2026-07-14T10:00:00Z', sha256: 'b'.repeat(64), last_observed_at: '2026-07-14T10:00:00Z' },
      ],
      updates: [],
      rule_changes: [{ observed_at: '2026-07-14T10:05:00Z', actor: 'admin', action: 'policy_rule_create', subject: 'GEOSITE: openai', succeeded: true, revision_number: null }],
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={client}><UpdatesPage /></QueryClientProvider>)

    expect(await screen.findByText('GeoSite · geosite.dat')).toBeVisible()
    expect(screen.getByText('GeoIP · geoip.dat')).toBeVisible()
    expect(screen.getByText('GEOSITE: openai')).toBeVisible()
    expect(screen.getByText('admin')).toBeVisible()
  })
})
