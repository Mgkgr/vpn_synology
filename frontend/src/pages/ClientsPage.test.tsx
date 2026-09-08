import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { ClientsPage } from './ClientsPage'

const client = {
  id: 42,
  name: 'laptop',
  enabled: true,
  ipv4_address: '10.66.0.2',
  latest_handshake_at: null,
  received_bytes: 999,
  transmitted_bytes: 999,
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('ClientsPage', () => {
  it('opens a client QR code in a dialog instead of a new browser tab', async () => {
    vi.spyOn(api, 'clients').mockResolvedValue([client])
    vi.spyOn(api, 'trafficUsage').mockResolvedValue({ period: 'month', usage: [] })
    vi.spyOn(api, 'wgeasyCredentialStatus').mockResolvedValue({ configured: true })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={queryClient}><ClientsPage /></QueryClientProvider>)

    expect(await screen.findByRole('link', { name: 'Скачать конфиг laptop' })).toHaveAttribute('download', 'laptop.conf')
    fireEvent.click(await screen.findByRole('button', { name: 'Показать QR-код laptop' }))

    const dialog = screen.getByRole('dialog', { name: 'QR-код профиля laptop' })
    expect(dialog).toBeVisible()
    expect(screen.getByRole('img', { name: 'QR-код профиля laptop' })).toHaveAttribute('src', '/api/clients/42/qr')
    fireEvent.click(screen.getByRole('button', { name: 'Закрыть QR-код' }))
    expect(screen.queryByRole('dialog', { name: 'QR-код профиля laptop' })).not.toBeInTheDocument()
  })

  it('отправляет безопасное отключение профиля и показывает учтённый месячный трафик', async () => {
    const disable = vi.spyOn(api, 'disableClient').mockResolvedValue(undefined)
    vi.spyOn(api, 'clients').mockResolvedValue([client])
    vi.spyOn(api, 'trafficUsage').mockResolvedValue({
      period: 'month',
      usage: [{ peer_id: '42', peer_name: 'laptop', received_bytes: 200, transmitted_bytes: 100 }],
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={queryClient}><ClientsPage /></QueryClientProvider>)

    expect(await screen.findByText('300 Б')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Отключить laptop' }))
    await waitFor(() => expect(disable.mock.calls[0]?.[0]).toBe(42))
  })

  it('подтверждает удаление в диалоге панели, а не системным уведомлением браузера', async () => {
    const remove = vi.spyOn(api, 'deleteClient').mockResolvedValue(undefined)
    vi.spyOn(api, 'clients').mockResolvedValue([client])
    vi.spyOn(api, 'trafficUsage').mockResolvedValue({ period: 'month', usage: [] })
    vi.spyOn(api, 'wgeasyCredentialStatus').mockResolvedValue({ configured: true })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={queryClient}><ClientsPage /></QueryClientProvider>)

    fireEvent.click(await screen.findByRole('button', { name: 'Удалить laptop' }))
    expect(screen.getByRole('dialog', { name: 'Удалить профиль laptop' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Удалить навсегда' }))
    await waitFor(() => expect(remove.mock.calls[0]?.[0]).toBe(42))
  })

  it('allows the owner to verify and save wg-easy credentials before refreshing clients', async () => {
    const clients = vi.spyOn(api, 'clients').mockResolvedValue([client])
    vi.spyOn(api, 'trafficUsage').mockResolvedValue({ period: 'month', usage: [] })
    vi.spyOn(api, 'wgeasyCredentialStatus').mockResolvedValue({ configured: false })
    const configure = vi.spyOn(api, 'configureWgEasy').mockResolvedValue({ configured: true })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={queryClient}><ClientsPage /></QueryClientProvider>)

    const configureButtons = await screen.findAllByRole('button', { name: 'Настроить wg-easy' })
    fireEvent.click(configureButtons[configureButtons.length - 1])
    fireEvent.change(screen.getByLabelText('Имя пользователя wg-easy'), { target: { value: 'owner' } })
    fireEvent.change(screen.getByLabelText('Пароль wg-easy'), { target: { value: 'credential-passphrase' } })
    fireEvent.click(screen.getByRole('button', { name: 'Проверить и сохранить' }))

    await waitFor(() => expect(configure).toHaveBeenCalledWith('owner', 'credential-passphrase'))
    await waitFor(() => expect(clients).toHaveBeenCalledTimes(2))
    expect(queryClient.getMutationCache().getAll()).toEqual([])
    expect(screen.queryByLabelText('Пароль wg-easy')).not.toBeInTheDocument()
  })

  it('отличает подключённый профиль от неактивного, не подключавшегося и отключённого', async () => {
    const now = Date.now()
    vi.spyOn(api, 'clients').mockResolvedValue([
      { ...client, id: 1, name: 'active', latest_handshake_at: new Date(now - 2 * 60_000).toISOString() },
      { ...client, id: 2, name: 'stale', latest_handshake_at: new Date(now - 20 * 60_000).toISOString() },
      { ...client, id: 3, name: 'never', latest_handshake_at: null },
      { ...client, id: 4, name: 'disabled', enabled: false, latest_handshake_at: new Date(now - 2 * 60_000).toISOString() },
    ])
    vi.spyOn(api, 'trafficUsage').mockResolvedValue({ period: 'month', usage: [] })
    vi.spyOn(api, 'wgeasyCredentialStatus').mockResolvedValue({ configured: true })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={queryClient}><ClientsPage /></QueryClientProvider>)

    const active = (await screen.findByText('active')).closest('tr')
    const stale = screen.getByText('stale').closest('tr')
    const never = screen.getByText('never').closest('tr')
    const disabled = screen.getByText('disabled').closest('tr')
    expect(active).not.toBeNull()
    expect(stale).not.toBeNull()
    expect(never).not.toBeNull()
    expect(disabled).not.toBeNull()
    expect(within(active!).getByText('подключён')).toBeVisible()
    expect(within(stale!).getByText(/неактивен · 20 мин/)).toBeVisible()
    expect(within(never!).getByText('не подключался')).toBeVisible()
    expect(within(disabled!).getByText('отключён')).toBeVisible()
  })
})
