import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { ComponentsPage } from './ComponentsPage'

const revision = 'a'.repeat(64)
const jobId = 'b'.repeat(32)
const inventory = { available: true, can_maintain: true, can_check: true, revision, components: [
  { component: 'wireguard', installed: true, running: true, actual_version: '15.2.2', actual_digest: 'sha256:' + revision, expected_digest: 'sha256:' + revision, checked_at: null, freshness_error: null, artifacts: [], release: null },
  { component: 'mihomo', installed: true, running: true, actual_version: 'v1.19.28', actual_digest: null, expected_digest: null, checked_at: null, freshness_error: null, artifacts: [], release: null },
] }

function mount(path = '/system/components') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><ComponentsPage /></MemoryRouter></QueryClientProvider>)
  return client
}

beforeEach(() => {
  vi.spyOn(api, 'maintenanceComponents').mockResolvedValue(inventory as never)
  vi.spyOn(api, 'maintenanceJobs').mockResolvedValue({ available: true, jobs: [] })
  vi.spyOn(api, 'maintenanceJob').mockResolvedValue({ job_id: jobId, phase: 'unknown', cancel_allowed: false })
  vi.spyOn(api, 'authorizeMaintenance').mockResolvedValue({ grant: 'ephemeral', expires_in: 300 })
  vi.spyOn(api, 'submitMaintenance').mockResolvedValue({ job_id: jobId, phase: 'queued', cancel_allowed: true })
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

describe('ComponentsPage', () => {
  it('shows_unknown_update_freshness without an upstream check on mount', async () => {
    const check = vi.spyOn(api, 'checkComponentReleases')
    mount()
    expect(await screen.findByRole('heading', { name: 'Компоненты' })).toBeVisible()
    expect(await screen.findAllByText('Обновления ещё не проверялись')).toHaveLength(2)
    expect(check).not.toHaveBeenCalled()
  })

  it('asks_owner_password_and_shows_affected_services', async () => {
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Перезапустить WireGuard / wg-easy' }))
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText(/Mihomo/)).toBeVisible()
    expect(within(dialog).getByLabelText('Пароль владельца')).toHaveFocus()
    expect(within(dialog).getByRole('button', { name: 'Подтвердить' })).toBeDisabled()
    expect(api.submitMaintenance).not.toHaveBeenCalled()
  })

  it('duplicate_click_reuses_job_id and clears password', async () => {
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Перезапустить Mihomo' }))
    fireEvent.change(screen.getByLabelText('Пароль владельца'), { target: { value: 'secret-for-test' } })
    const button = screen.getByRole('button', { name: 'Подтвердить' })
    fireEvent.click(button); fireEvent.click(button)
    await waitFor(() => expect(api.submitMaintenance).toHaveBeenCalledTimes(1))
    expect(api.authorizeMaintenance).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.queryByLabelText('Пароль владельца')).not.toBeInTheDocument())
    expect(JSON.stringify(vi.mocked(api.submitMaintenance).mock.calls)).not.toContain('secret-for-test')
  })

  it('reconnect_restores_job_without_resubmit', async () => {
    mount('/system/components?job=' + jobId)
    expect(await screen.findByText('Результат пока не подтверждён')).toBeVisible()
    expect(api.maintenanceJob).toHaveBeenCalledWith(jobId)
    expect(api.submitMaintenance).not.toHaveBeenCalled()
    expect(api.authorizeMaintenance).not.toHaveBeenCalled()
  })

  it('stopped_service_is_not_selected_silently', async () => {
    vi.mocked(api.maintenanceComponents).mockResolvedValue({ ...inventory, components: [{ ...inventory.components[0], running: false }] } as never)
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Перезапустить WireGuard / wg-easy' }))
    const choice = screen.getByRole('checkbox', { name: /Разрешить запуск остановленного/ })
    expect(choice).not.toBeChecked()
    fireEvent.change(screen.getByLabelText('Пароль владельца'), { target: { value: 'secret-for-test' } })
    expect(screen.getByRole('button', { name: 'Подтвердить' })).toBeDisabled()
  })

  it('unsupported_release_is_disabled and admin is readonly', async () => {
    vi.mocked(api.maintenanceComponents).mockResolvedValue({ ...inventory, can_maintain: false } as never)
    mount()
    expect(await screen.findByRole('button', { name: 'Перезапустить Mihomo' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Обновить Mihomo' })).toBeDisabled()
  })

  it('lost_post_response_never_retries_the_mutation', async () => {
    vi.mocked(api.submitMaintenance).mockRejectedValue(new TypeError('Failed to fetch'))
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Перезапустить Mihomo' }))
    fireEvent.change(screen.getByLabelText('Пароль владельца'), { target: { value: 'secret-for-test' } })
    fireEvent.click(screen.getByRole('button', { name: 'Подтвердить' }))
    expect(await screen.findByText(/Ответ потерян/)).toBeVisible()
    expect(api.submitMaintenance).toHaveBeenCalledTimes(1)
  })

  it('rollback_warns_about_newer_data and requires explicit acceptance', async () => {
    vi.mocked(api.maintenanceComponents).mockResolvedValue({ ...inventory, backup_snapshots: [{ snapshot_id: 'c'.repeat(64), components: ['mihomo'], verified_at: 1789990000, captured_revision: 'd'.repeat(64) }] } as never)
    mount()
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Выбрать Mihomo' }))
    fireEvent.change(screen.getByLabelText('Копия для восстановления'), { target: { value: 'c'.repeat(64) } })
    fireEvent.click(screen.getByRole('button', { name: 'Восстановить выбранные из копии' }))
    expect(screen.getByText(/может удалить клиентов и изменения/)).toBeVisible()
    fireEvent.change(screen.getByLabelText('Пароль владельца'), { target: { value: 'secret-for-test' } })
    expect(screen.getByRole('button', { name: 'Подтвердить' })).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: /Подтверждаю потерю/ }))
    expect(screen.getByRole('button', { name: 'Подтвердить' })).toBeEnabled()
  })

  it('manual_release_check_cooldown_expires_without_navigation', async () => {
    vi.spyOn(api, 'checkComponentReleases').mockResolvedValue({ queued: true })
    mount()
    const check = await screen.findByRole('button', { name: 'Проверить версии' })
    await waitFor(() => expect(check).toBeEnabled())
    vi.useFakeTimers()
    await act(async () => { fireEvent.click(check) })
    expect(check).toBeDisabled()
    await act(async () => { vi.advanceTimersByTime(60_001) })
    expect(check).toBeEnabled()
  })
})
