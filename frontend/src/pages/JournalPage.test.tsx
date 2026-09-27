import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { JournalPage } from './JournalPage'

afterEach(() => vi.restoreAllMocks())

describe('JournalPage', () => {
  it('передаёт фильтр выхода серверу вместо поиска по actor или action', async () => {
    const journal = vi.spyOn(api, 'journal').mockResolvedValue({ events: [], page: 1, page_size: 50, has_more: false })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={client}><JournalPage /></QueryClientProvider>)

    fireEvent.change(screen.getByPlaceholderText('VLESS-NL'), { target: { value: 'HY2-DE' } })
    await waitFor(() => expect(journal).toHaveBeenLastCalledWith(expect.objectContaining({ outbound: 'HY2-USA' })))
    fireEvent.change(screen.getByPlaceholderText('VLESS-NL'), { target: { value: 'VLESS-NL' } })
    await waitFor(() => expect(journal).toHaveBeenLastCalledWith(expect.objectContaining({ outbound: 'WG-IMP' })))
  })
})
