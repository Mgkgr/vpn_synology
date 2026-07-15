import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { AdministratorsPage } from './AdministratorsPage'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('AdministratorsPage', () => {
  it('показывает администраторов и действие отзыва сессий', async () => {
    vi.spyOn(api, 'administrators').mockResolvedValue([{ username: 'owner', bootstrap_owner: true }, { username: 'artem', bootstrap_owner: false }])
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><AdministratorsPage /></QueryClientProvider>)
    expect(await screen.findByText('owner')).toBeVisible()
    expect(screen.getByText('artem')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Отозвать сессии artem' })).toBeVisible()
  })
})
