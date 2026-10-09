import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { OutboundProfilesPage } from './OutboundProfilesPage'

const revision = 'a'.repeat(64), draftId = 'd'.repeat(32), jobId = 'b'.repeat(32)
const draft = () => ({ target: 'WG-IMP', protocol: 'vless', server: 'vpn.example', port: 443, draft_id: draftId, revision,
  created_at: Date.now() / 1000, expires_at: Date.now() / 1000 + 1800, checked_at: null, check_passed: false, endpoint_ip: null, results: [] })
const snapshot = () => ({ available: true, can_manage: true, revision, current: [{ target: 'WG-IMP', protocol: 'vless', server: 'old.example', port: 443 }], drafts: [] })
function mount(path = '/routes/profiles') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><OutboundProfilesPage /></MemoryRouter></QueryClientProvider>)
  return client
}
beforeEach(() => {
  vi.spyOn(api, 'outboundProfiles').mockResolvedValue(snapshot() as never)
  vi.spyOn(api, 'previewOutboundProfile').mockResolvedValue(draft() as never)
  vi.spyOn(api, 'maintenanceJobs').mockResolvedValue({ available: true, jobs: [] })
  vi.spyOn(api, 'maintenanceJob').mockImplementation(async id => ({ job_id: id, phase: 'unknown', cancel_allowed: false }))
  vi.spyOn(api, 'authorizeMaintenance').mockResolvedValue({ grant: 'one-use', expires_in: 300 })
  vi.spyOn(api, 'submitMaintenance').mockImplementation(async id => ({ job_id: id, phase: 'queued', cancel_allowed: true }))
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('pastes privately, clears raw link after preview and never automatically probes', async () => {
  mount()
  const input = await screen.findByLabelText('Ссылка подключения')
  expect(input).toHaveAttribute('type', 'password')
  fireEvent.change(input, { target: { value: 'vless://synthetic-link' } })
  fireEvent.click(screen.getByRole('button', { name: 'Разобрать ссылку' }))
  expect(await screen.findByText('vpn.example:443')).toBeVisible()
  expect(input).toHaveValue('')
  expect(api.submitMaintenance).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: 'Применить ключ' })).toBeDisabled()
  expect(localStorage.length).toBe(0); expect(sessionStorage.length).toBe(0)
})

it('test confirmation submits only draft reference and focuses password', async () => {
  vi.mocked(api.outboundProfiles).mockResolvedValue({ ...snapshot(), drafts: [draft()] } as never)
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Проверить ключ' }))
  const dialog = screen.getByRole('dialog')
  expect(within(dialog).getByLabelText('Пароль владельца')).toHaveFocus()
  fireEvent.change(screen.getByLabelText('Пароль владельца'), { target: { value: 'test-owner-password' } })
  fireEvent.click(within(dialog).getByRole('button', { name: 'Подтвердить' }))
  await waitFor(() => expect(api.submitMaintenance).toHaveBeenCalledTimes(1))
  expect(vi.mocked(api.submitMaintenance).mock.calls[0][1]).toEqual({ action: 'profile_check', draft_id: draftId, expected_revision: revision })
  expect(JSON.stringify(vi.mocked(api.submitMaintenance).mock.calls)).not.toContain('test-owner-password')
  expect(await screen.findByText('В очереди')).toBeVisible()
})

it('allows apply only for fresh successful check and warns about reload', async () => {
  vi.mocked(api.outboundProfiles).mockResolvedValue({ ...snapshot(), drafts: [{ ...draft(), check_passed: true, checked_at: Date.now() / 1000, endpoint_ip: '1.1.1.1' }] } as never)
  mount()
  expect(await screen.findByRole('button', { name: 'Применить ключ' })).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: 'Применить ключ' }))
  expect(within(screen.getByRole('dialog')).getByText(/краткое переподключение/)).toBeVisible()
  fireEvent.change(screen.getByLabelText('Пароль владельца'), { target: { value: 'test-owner-password' } })
  fireEvent.click(screen.getByRole('button', { name: 'Подтвердить' }))
  await waitFor(() => expect(api.submitMaintenance).toHaveBeenCalledTimes(1))
  expect(vi.mocked(api.submitMaintenance).mock.calls[0][1]).toMatchObject({ action: 'profile_apply', draft_id: draftId })
})

it('does not allow stale or revision-mismatched evidence to apply', async () => {
  vi.mocked(api.outboundProfiles).mockResolvedValue({ ...snapshot(), revision: 'f'.repeat(64), drafts: [{ ...draft(), check_passed: true, checked_at: Date.now() / 1000 - 400, endpoint_ip: '1.1.1.1' }] } as never)
  mount()
  expect(await screen.findByRole('button', { name: 'Применить ключ' })).toBeDisabled()
  expect(screen.getByText(/Настройки изменились/)).toBeVisible()
})

it('lost response polls the previous job without duplicate submission', async () => {
  vi.mocked(api.outboundProfiles).mockResolvedValue({ ...snapshot(), drafts: [draft()] } as never)
  vi.mocked(api.submitMaintenance).mockRejectedValue(new TypeError('Failed to fetch'))
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Проверить ключ' }))
  fireEvent.change(screen.getByLabelText('Пароль владельца'), { target: { value: 'test-owner-password' } })
  fireEvent.click(screen.getByRole('button', { name: 'Подтвердить' }))
  expect(await screen.findByText(/Ответ потерян/)).toBeVisible()
  expect(api.submitMaintenance).toHaveBeenCalledTimes(1)
  await waitFor(() => expect(api.maintenanceJob).toHaveBeenCalled())
})

it('read-only admin cannot import and terminal job refreshes current profile', async () => {
  vi.mocked(api.outboundProfiles).mockResolvedValue({ ...snapshot(), can_manage: false } as never)
  vi.mocked(api.maintenanceJob).mockResolvedValue({ job_id: jobId, phase: 'completed', cancel_allowed: false })
  mount('/routes/profiles?job=' + jobId)
  expect(await screen.findByText('Завершено')).toBeVisible()
  expect(screen.queryByLabelText('Ссылка подключения')).not.toBeInTheDocument()
  expect(screen.getByText(/Замена доступна только владельцу/)).toBeVisible()
  await waitFor(() => expect(vi.mocked(api.outboundProfiles).mock.calls.length).toBeGreaterThan(1))
  expect(api.submitMaintenance).not.toHaveBeenCalled()
})
