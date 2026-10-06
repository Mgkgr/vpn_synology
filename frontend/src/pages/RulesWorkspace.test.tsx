import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api } from '../api/client'
import type { RulesResponse } from '../api/types'
import { RulesPage } from './RulesPage'

let state: RulesResponse
beforeEach(() => {
  state = {
    rules: [], providers: [], direct_text: 'DOMAIN,exact.example,DIRECT\nIP-CIDR,192.168.3.0/24,DIRECT,no-resolve\n',
    policies: [{ id: 12, kind: 'GEOSITE', category: 'openai', label: 'OpenAI / ChatGPT', action: 'VPS-FALLBACK', enabled: true }],
    policy_catalog: [
      { kind: 'GEOSITE', category: 'openai', label: 'OpenAI / ChatGPT' },
      { kind: 'GEOSITE', category: 'ozon', label: 'Ozon' },
      { kind: 'GEOIP', category: 'US', label: 'США — IP-сети' },
    ],
  }
  vi.spyOn(api, 'rules').mockImplementation(async () => structuredClone(state))
  vi.spyOn(api, 'updates').mockResolvedValue({ assets: [], updates: [] })
  vi.spyOn(api, 'siteProbes').mockResolvedValue({ enabled: false, timezone: 'Asia/Yekaterinburg', next_run_at: null, run: null, services: [] })
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><RulesPage /></QueryClientProvider>)
  return client
}

describe('rules workspace', () => {
  it('reads one cached snapshot and does not probe or write when expanding a tile', async () => {
    const client = setup()
    fireEvent.click(await screen.findByRole('button', { name: /OpenAI \/ ChatGPT/ }))
    expect(await screen.findByText(/Суточные замеры выключены/)).toBeVisible()
    expect(api.siteProbes).toHaveBeenCalledOnce()
    const query = client.getQueryCache().find({ queryKey: ['service-checks'] })!
    expect(query.options).toMatchObject({ staleTime: 60_000, refetchInterval: 60_000, refetchIntervalInBackground: false })
    fireEvent.click(screen.getByRole('button', { name: /OpenAI \/ ChatGPT/ }))
    expect(api.siteProbes).toHaveBeenCalledOnce()
  })

  it('keeps route editing available when the service-check snapshot fails', async () => {
    vi.mocked(api.siteProbes).mockRejectedValue(new Error('offline'))
    setup()
    expect(await screen.findByText(/Суточные замеры недоступны/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: /Ozon/ }))
    expect(screen.getByRole('button', { name: 'Применить' })).toBeEnabled()
  })

  it('marks cached successful service observations unconfirmed after a failed refresh', async () => {
    vi.mocked(api.siteProbes).mockResolvedValue({ enabled: true, timezone: 'Asia/Yekaterinburg', next_run_at: null, run: null, services: [{ key: 'openai', category: 'openai', url: 'https://chatgpt.com/', routes: [{ route_id: 'direct', label: 'DIRECT', state: 'responded', delay_ms: 123, reason: null, observed_at: '2026-10-04T00:00:00Z', url: 'https://chatgpt.com/', stale: false, current_run: true }] }] })
    const client = setup()
    expect(await screen.findByText('DIRECT · 123 мс')).toHaveAttribute('data-probe-tone', 'good')
    vi.mocked(api.siteProbes).mockRejectedValue(new Error('offline'))
    await act(async () => { await client.invalidateQueries({ queryKey: ['service-checks'] }) })
    expect(await screen.findByText('DIRECT · не подтверждено')).toHaveAttribute('data-probe-tone', 'muted')
    fireEvent.click(screen.getByRole('button', { name: /OpenAI \/ ChatGPT/ }))
    expect(screen.getByLabelText('Маршрут')).toBeEnabled()
  })

  it('does not attach a domain probe to a GEOIP category with the same identifier', async () => {
    state.policy_catalog!.push({ kind: 'GEOIP', category: 'openai', label: 'Только IP тест' })
    vi.mocked(api.siteProbes).mockResolvedValue({ enabled: true, timezone: 'Asia/Yekaterinburg', next_run_at: null, run: null, services: [{ key: 'openai', category: 'openai', url: 'https://chatgpt.com/', routes: [{ route_id: 'direct', label: 'DIRECT', state: 'responded', delay_ms: 123, reason: null, observed_at: '2026-10-04T00:00:00Z', url: 'https://chatgpt.com/', stale: false, current_run: true }] }] })
    setup()
    const tile = await screen.findByRole('button', { name: /Только IP тест/ })
    expect(await screen.findByText('DIRECT · 123 мс')).toBeVisible()
    expect(within(tile.parentElement!).getByText('Для категории нет единого контрольного сайта')).toBeVisible()
    expect(within(tile.parentElement!).queryByText('DIRECT · 123 мс')).not.toBeInTheDocument()
  })

  it('opens an inline route choice without writing, then updates the existing policy', async () => {
    const create = vi.spyOn(api, 'createManagedRule')
    const update = vi.spyOn(api, 'updateManagedRule').mockImplementation(async (id, payload) => {
      const changed = { ...state.policies![0], ...payload, id }
      state = { ...state, policies: [changed] }
      return changed
    })
    setup()
    const tile = await screen.findByRole('button', { name: /OpenAI \/ ChatGPT/ })
    expect(tile).toHaveAttribute('aria-expanded', 'false')
    expect(tile).toHaveClass('is-active')
    fireEvent.click(tile)
    expect(tile).toHaveAttribute('aria-expanded', 'true')
    expect(tile).toHaveClass('selected')
    expect(update).not.toHaveBeenCalled()
    const menu = screen.getByRole('group', { name: 'Маршрут: OpenAI / ChatGPT' })
    fireEvent.change(within(menu).getByLabelText('Маршрут'), { target: { value: 'HY2-USA' } })
    fireEvent.click(within(menu).getByRole('button', { name: 'Применить' }))
    await waitFor(() => expect(update).toHaveBeenCalledWith(12, { kind: 'GEOSITE', category: 'openai', action: 'HY2-USA', enabled: true }))
    expect(create).not.toHaveBeenCalled()
    expect(await screen.findByText('Назначено: HY2-DE')).toBeVisible()
    expect(screen.queryByLabelText('Маршрут правила 12')).not.toBeInTheDocument()
  })

  it('can disable, re-enable and remove a category from the tile', async () => {
    vi.spyOn(api, 'updateManagedRule').mockImplementation(async (id, payload) => {
      const changed = { ...state.policies![0], ...payload, id }
      state = { ...state, policies: [changed] }
      return changed
    })
    const remove = vi.spyOn(api, 'deleteManagedRule').mockImplementation(async () => { state = { ...state, policies: [] } })
    setup()
    fireEvent.click(await screen.findByRole('button', { name: /OpenAI \/ ChatGPT/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Отключить правило' }))
    expect(await screen.findByText('Правило отключено')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Включить правило' }))
    expect(await screen.findByText('Назначено: Авто VPN')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Убрать назначение' }))
    expect(remove).not.toHaveBeenCalled()
    const confirmation = screen.getByRole('group', { name: 'Подтверждение удаления правила' })
    fireEvent.click(within(confirmation).getByRole('button', { name: 'Удалить' }))
    await waitFor(() => expect(remove).toHaveBeenCalledWith(12))
    expect(await screen.findAllByText('По общим правилам')).not.toHaveLength(0)
  })

  it('reconciles a lost update response without repeating a write', async () => {
    const update = vi.spyOn(api, 'updateManagedRule').mockImplementation(async (id, payload) => {
      state = { ...state, policies: [{ ...state.policies![0], ...payload, id }] }
      throw new TypeError('Failed to fetch')
    })
    setup()
    fireEvent.click(await screen.findByRole('button', { name: /OpenAI \/ ChatGPT/ }))
    fireEvent.change(screen.getByLabelText('Маршрут'), { target: { value: 'DIRECT' } })
    fireEvent.click(screen.getByRole('button', { name: 'Применить' }))
    expect(await screen.findByText(/Ответ потерян.*сохранённое состояние подтверждено/, {}, { timeout: 3000 })).toBeVisible()
    expect(update).toHaveBeenCalledOnce()
    expect(screen.queryByText('Failed to fetch')).not.toBeInTheDocument()
  })

  it('shows domains without rule syntax and preserves exact domains and technical rules when adding a site', async () => {
    state = { ...state, direct_sha256: 'b'.repeat(64) }
    const apply = vi.spyOn(api, 'applyDirectRules').mockImplementation(async (text) => {
      state = { ...state, direct_text: text }
      return { revision_number: 4, sha256: 'a'.repeat(64) }
    })
    setup()
    expect(await screen.findByText('exact.example')).toBeVisible()
    expect(screen.queryByLabelText('Правила DIRECT')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Новый сайт DIRECT'), { target: { value: 'example.org' } })
    fireEvent.click(screen.getByRole('button', { name: 'Добавить сайт' }))
    fireEvent.click(screen.getByRole('button', { name: /Применить DIRECT/ }))
    await waitFor(() => expect(apply).toHaveBeenCalledOnce())
    expect(apply.mock.calls[0][0]).toBe('DOMAIN,exact.example,DIRECT\nIP-CIDR,192.168.3.0/24,DIRECT,no-resolve\nDOMAIN-SUFFIX,example.org,DIRECT\n')
    expect(apply.mock.calls[0][1]).toBe('b'.repeat(64))
  })

  it('preserves a DIRECT draft and blocks overwriting a concurrently changed server file', async () => {
    const client = setup()
    await screen.findByText('exact.example')
    fireEvent.change(screen.getByLabelText('Новый сайт DIRECT'), { target: { value: 'draft.example' } })
    fireEvent.click(screen.getByRole('button', { name: 'Добавить сайт' }))
    state = { ...state, direct_text: `${state.direct_text}DOMAIN,other.example,DIRECT\n` }
    await act(async () => { await client.invalidateQueries({ queryKey: ['rules'] }) })
    expect(screen.getByText('draft.example')).toBeVisible()
    expect(await screen.findByText(/На сервере список изменился/)).toBeVisible()
    expect(screen.getByRole('button', { name: /Применить DIRECT/ })).toBeDisabled()
  })

  it('does not enable DIRECT editing from an unreadable partial response', async () => {
    state = { ...state, direct_text: null, section_errors: { direct: 'DIRECT-список недоступен.' } }
    setup()
    expect(await screen.findByText('DIRECT-список недоступен.')).toBeVisible()
    expect(screen.getByLabelText('Новый сайт DIRECT')).toBeDisabled()
    expect(screen.getByRole('button', { name: /OpenAI \/ ChatGPT/ })).toBeEnabled()
  })

  it('shows file time separately from observation and does not claim source or loaded-version health', async () => {
    vi.mocked(api.updates).mockResolvedValue({ assets: [
      { filename: 'GeoSite.dat', kind: 'GeoSite', size_bytes: 123, sha256: 'a'.repeat(64), modified_at: '2026-10-01T04:00:00Z', last_observed_at: '2026-10-04T04:00:00Z' },
    ], updates: [{ id: 2, observed_at: '2026-10-04T04:00:00Z', source: 'scheduled', operation: 'geo_upgrade', succeeded: false, status_code: 504, version: null, verification: 'failed', checked_files: [], changed_files: [] }] })
    setup()
    const card = await screen.findByRole('region', { name: 'Состояние GeoSite' })
    expect(await within(card).findByText('Файл доступен')).toBeVisible()
    expect(within(card).getByText('Дата файла').nextElementSibling?.querySelector('time')).toHaveAttribute('dateTime', '2026-10-01T04:00:00Z')
    fireEvent.click(within(card).getByText('Источник и применение'))
    expect(within(card).getByText(/Источники отдельно не проверены/)).toBeVisible()
    expect(within(card).getByText(/Версия в памяти Mihomo не подтверждена/)).toBeVisible()
    expect(screen.queryByText(/прежние файлы сохранены/)).not.toBeInTheDocument()
  })

  it('does not invent a verification phase while the update request is still pending', async () => {
    vi.spyOn(api, 'updateGeo').mockReturnValue(new Promise(() => {}))
    setup()
    fireEvent.click(screen.getByRole('button', { name: 'Обновить GeoSite / GeoIP' }))
    await new Promise((resolve) => setTimeout(resolve, 900))
    expect(screen.queryByText(/Mihomo ответил, сверяем/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Обновление выполняется/ })).toBeDisabled()
  })

  it('does not confirm a lost delete from a response with no policy section', async () => {
    const remove = vi.spyOn(api, 'deleteManagedRule').mockRejectedValue(new TypeError('Failed to fetch'))
    setup()
    fireEvent.click(await screen.findByRole('button', { name: /OpenAI \/ ChatGPT/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Убрать назначение' }))
    vi.mocked(api.rules).mockResolvedValue({ rules: [], providers: [], direct_text: '' })
    fireEvent.click(within(screen.getByRole('group', { name: 'Подтверждение удаления правила' })).getByRole('button', { name: 'Удалить' }))
    expect(await screen.findByText(/Результат не подтверждён/, {}, { timeout: 4000 })).toBeVisible()
    expect(screen.queryByText(/сохранённое состояние подтверждено/)).not.toBeInTheDocument()
    expect(remove).toHaveBeenCalledOnce()
  })

  it('keeps a draft after a revision conflict and never retries the write', async () => {
    const apply = vi.spyOn(api, 'applyDirectRules').mockRejectedValue(new ApiError(409, 'DIRECT-список изменился.'))
    setup()
    await screen.findByText('exact.example')
    fireEvent.change(screen.getByLabelText('Новый сайт DIRECT'), { target: { value: 'draft.example' } })
    fireEvent.click(screen.getByRole('button', { name: 'Добавить сайт' }))
    fireEvent.click(screen.getByRole('button', { name: /Применить DIRECT/ }))
    expect(await screen.findByText('DIRECT-список изменился.')).toBeVisible()
    expect(screen.getByText('draft.example')).toBeVisible()
    expect(apply).toHaveBeenCalledOnce()
  })

  it('prevents another gateway write while a category change is in flight', async () => {
    vi.spyOn(api, 'updateManagedRule').mockReturnValue(new Promise(() => {}))
    setup()
    fireEvent.click(await screen.findByRole('button', { name: /OpenAI \/ ChatGPT/ }))
    fireEvent.change(screen.getByLabelText('Маршрут'), { target: { value: 'DIRECT' } })
    fireEvent.click(screen.getByRole('button', { name: 'Применить' }))
    await screen.findByText('Сохраняем назначение… Не повторяйте запрос.')
    expect(screen.getByRole('button', { name: 'Обновить GeoSite / GeoIP' })).toBeDisabled()
    expect(screen.getByLabelText('Новый сайт DIRECT')).toBeDisabled()
  })

  it('does not leave controls locked because of an old pending update followed by a completed attempt', async () => {
    const base = { source: 'manual', operation: 'geo_upgrade', status_code: null, version: null, checked_files: [], changed_files: [] }
    vi.mocked(api.updates).mockResolvedValue({ updates: [
      { ...base, id: 3, observed_at: '2026-10-04T04:00:00Z', succeeded: false, verification: 'failed' },
      { ...base, id: 2, observed_at: '2026-10-01T04:00:00Z', succeeded: null, verification: 'pending' },
    ] })
    setup()
    await screen.findByText(/Обновление не подтверждено/)
    expect(screen.getByRole('button', { name: 'Обновить GeoSite / GeoIP' })).toBeEnabled()
  })

  it('marks cached geodata as stale after a failed refresh', async () => {
    vi.mocked(api.updates).mockResolvedValue({ assets: [
      { filename: 'GeoSite.dat', kind: 'GeoSite', size_bytes: 123, sha256: 'a'.repeat(64), modified_at: '2026-10-01T04:00:00Z', last_observed_at: null },
    ], updates: [] })
    const client = setup()
    await screen.findByText('Файл доступен')
    vi.mocked(api.updates).mockRejectedValue(new Error('offline'))
    await act(async () => { await client.invalidateQueries({ queryKey: ['updates'] }) })
    expect(await screen.findByText('Снимок устарел; доступность не подтверждена')).toBeVisible()
    expect(screen.queryByText('Файл доступен')).not.toBeInTheDocument()
  })

  it('does not show an unreadable DIRECT file as empty or repeat an old success notice', async () => {
    vi.spyOn(api, 'applyDirectRules').mockImplementation(async (text) => {
      state = { ...state, direct_text: text }
      return { revision_number: 5, sha256: 'a'.repeat(64) }
    })
    const client = setup()
    await screen.findByText('exact.example')
    fireEvent.change(screen.getByLabelText('Новый сайт DIRECT'), { target: { value: 'new.example' } })
    fireEvent.click(screen.getByRole('button', { name: 'Добавить сайт' }))
    fireEvent.click(screen.getByRole('button', { name: /Применить DIRECT/ }))
    await screen.findByText('DIRECT-правила сохранены. Ревизия #5.')
    state = { ...state, direct_text: null, section_errors: { direct: 'DIRECT-список недоступен.' } }
    await act(async () => { await client.invalidateQueries({ queryKey: ['rules'] }) })
    expect(await screen.findByText('Список недоступен')).toBeVisible()
    expect(screen.queryByText('0 сайтов')).not.toBeInTheDocument()
    expect(screen.queryByText('DIRECT-правила сохранены. Ревизия #5.')).not.toBeInTheDocument()
  })

  it('pins the original row when a server refresh happens during domain editing', async () => {
    const client = setup()
    fireEvent.click(await screen.findByRole('button', { name: 'exact.example' }))
    state = { ...state, direct_text: `DOMAIN,other.example,DIRECT\n${state.direct_text}` }
    await act(async () => { await client.invalidateQueries({ queryKey: ['rules'] }) })
    expect(await screen.findByText(/На сервере список изменился/)).toBeVisible()
    fireEvent.change(screen.getByLabelText('Новый сайт DIRECT'), { target: { value: 'renamed.example' } })
    fireEvent.click(screen.getByRole('button', { name: 'Изменить сайт' }))
    fireEvent.click(screen.getByText(/Технический режим/))
    expect(await screen.findByLabelText('Правила DIRECT')).toHaveValue('DOMAIN,renamed.example,DIRECT\nIP-CIDR,192.168.3.0/24,DIRECT,no-resolve\n')
    expect(screen.getByRole('button', { name: /Применить DIRECT/ })).toBeDisabled()
    expect(screen.getByText(/На сервере список изменился/)).toBeVisible()
  })

  it('pins a pending domain deletion instead of deleting another row after a refresh', async () => {
    const client = setup()
    fireEvent.click(await screen.findByRole('button', { name: 'Удалить exact.example' }))
    state = { ...state, direct_text: `DOMAIN,other.example,DIRECT\n${state.direct_text}` }
    await act(async () => { await client.invalidateQueries({ queryKey: ['rules'] }) })
    expect(await screen.findByText(/На сервере список изменился/)).toBeVisible()
    const confirmation = screen.getByRole('group', { name: 'Удаление exact.example' })
    fireEvent.click(within(confirmation).getByRole('button', { name: 'Удалить' }))
    fireEvent.click(screen.getByText(/Технический режим/))
    expect(await screen.findByLabelText('Правила DIRECT')).toHaveValue('IP-CIDR,192.168.3.0/24,DIRECT,no-resolve\n')
    expect(screen.getByRole('button', { name: /Применить DIRECT/ })).toBeDisabled()
  })
})
