import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { RulesPage } from './RulesPage'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('RulesPage', () => {
  it('обновляет GeoSite и GeoIP прямо из раздела правил и объясняет GeoIP-категории', async () => {
    const updateGeo = vi.spyOn(api, 'updateGeo').mockResolvedValue({ id: 1, observed_at: '2026-07-21T19:59:00Z', source: 'manual', operation: 'geo_upgrade', succeeded: true, status_code: 204, version: null, verification: 'unchanged', checked_files: ['GeoIP.dat', 'GeoSite.dat'], changed_files: [] })
    vi.spyOn(api, 'rules').mockResolvedValue({
      rules: [], providers: [], direct_text: '', policies: [],
      policy_catalog: [
        { kind: 'GEOSITE', category: 'proxy', label: 'VPN / прокси-инфраструктура', description: 'Домены VPN и прокси, а не все зарубежные сайты.' },
        { kind: 'GEOIP', category: 'US', label: 'США — IP-сети', description: 'Срабатывает по IP назначения; CDN может выбрать другой регион.' },
      ],
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={client}><RulesPage /></QueryClientProvider>)

    expect(await screen.findByText('Домены VPN и прокси, а не все зарубежные сайты.')).toBeVisible()
    expect(screen.getByText('Срабатывает по IP назначения; CDN может выбрать другой регион.')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Обновить GeoSite / GeoIP' }))
    await waitFor(() => expect(updateGeo).toHaveBeenCalledOnce())
    expect(screen.getByText('Готово: GeoIP.dat, GeoSite.dat уже актуальны. HTTP 204.')).toBeVisible()
  })

  it('показывает отдельный статус до загрузки GeoSite, GeoIP и правил Mihomo', () => {
    vi.spyOn(api, 'rules').mockReturnValue(new Promise(() => {}))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={client}><RulesPage /></QueryClientProvider>)

    expect(screen.getByText('Загружаем GeoSite, GeoIP и правила Mihomo…')).toBeVisible()
  })

  it('ждёт точный загруженный DIRECT-файл и не применяет неизменённый текст', async () => {
    let resolveRules: ((value: Awaited<ReturnType<typeof api.rules>>) => void) | undefined
    vi.spyOn(api, 'rules').mockReturnValue(new Promise((resolve) => { resolveRules = resolve }))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={client}><RulesPage /></QueryClientProvider>)

    const apply = screen.getByRole('button', { name: 'Применить DIRECT-правила' })
    expect(apply).toBeDisabled()
    resolveRules?.({
      rules: [],
      providers: [],
      direct_text: 'DOMAIN-SUFFIX,example.local,DIRECT\nIP-CIDR,192.168.0.0/16,DIRECT\n',
    })

    const editor = screen.getByLabelText('Правила DIRECT')
    await waitFor(() => expect(editor).toHaveValue('DOMAIN-SUFFIX,example.local,DIRECT\nIP-CIDR,192.168.0.0/16,DIRECT\n'))
    expect(apply).toBeDisabled()
    fireEvent.change(editor, { target: { value: 'DOMAIN-SUFFIX,example.local,DIRECT\n' } })
    expect(apply).toBeEnabled()
  })

  it('даёт выбрать популярное направление и способ маршрутизации без поиска по спискам', async () => {
    const create = vi.spyOn(api, 'createManagedRule').mockResolvedValue({ id: 7, kind: 'GEOSITE', category: 'anthropic', label: 'Claude / Anthropic', action: 'DIRECT', enabled: true })
    vi.spyOn(api, 'rules').mockResolvedValue({
      rules: [],
      providers: [],
      direct_text: '',
      policies: [],
      policy_catalog: [
        { kind: 'GEOSITE', category: 'openai', label: 'OpenAI / ChatGPT' },
        { kind: 'GEOSITE', category: 'anthropic', label: 'Claude / Anthropic' },
        { kind: 'GEOIP', category: 'US', label: 'США (IP)' },
      ],
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={client}><RulesPage /></QueryClientProvider>)

    fireEvent.click(await screen.findByRole('button', { name: /Claude \/ Anthropic/ }))
    fireEvent.click(screen.getByRole('button', { name: 'DIRECT в обход VPN' }))
    fireEvent.click(screen.getByRole('button', { name: 'Добавить правило' }))

    await waitFor(() => expect(create).toHaveBeenCalled())
    expect(create.mock.calls[0]?.[0]).toEqual({ kind: 'GEOSITE', category: 'anthropic', action: 'DIRECT', enabled: true })
  })

  it('отделяет проверенные российские GeoSite-направления и даёт назначить им DIRECT', async () => {
    const create = vi.spyOn(api, 'createManagedRule').mockResolvedValue({ id: 8, kind: 'GEOSITE', category: 'ozon', label: 'Ozon', action: 'DIRECT', enabled: true })
    vi.spyOn(api, 'rules').mockResolvedValue({
      rules: [],
      providers: [],
      direct_text: '',
      policies: [],
      policy_catalog: [
        { kind: 'GEOSITE', category: 'category-bank-ru', label: 'Банки и финансы РФ' },
        { kind: 'GEOSITE', category: 'ozon', label: 'Ozon' },
        { kind: 'GEOSITE', category: 'category-ru', label: 'Все сайты РФ — широкое правило' },
        { kind: 'GEOSITE', category: 'openai', label: 'OpenAI / ChatGPT' },
        { kind: 'GEOIP', category: 'US', label: 'США (IP)' },
      ],
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={client}><RulesPage /></QueryClientProvider>)

    expect(await screen.findByRole('heading', { name: 'Российские сервисы' })).toBeVisible()
    expect(screen.getByText(/Широкое правило направит весь набор/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: /^Ozon\b/ }))
    fireEvent.click(screen.getByRole('button', { name: 'DIRECT в обход VPN' }))
    fireEvent.click(screen.getByRole('button', { name: 'Добавить правило' }))

    await waitFor(() => expect(create.mock.calls[0]?.[0]).toEqual({ kind: 'GEOSITE', category: 'ozon', action: 'DIRECT', enabled: true }))
  })

  it('отделяет P2P, стриминг, игры и специальные GeoSite-категории без автоматического создания правила', async () => {
    const create = vi.spyOn(api, 'createManagedRule').mockResolvedValue({ id: 9, kind: 'GEOSITE', category: 'category-public-tracker', label: 'Публичные торрент-трекеры', action: 'VPS-FALLBACK', enabled: true })
    vi.spyOn(api, 'rules').mockResolvedValue({
      rules: [],
      providers: [],
      direct_text: '',
      policies: [],
      policy_catalog: [
        { kind: 'GEOSITE', category: 'category-public-tracker', label: 'Публичные торрент-трекеры', description: 'Сопоставляется по домену трекера; не распознаёт весь протокол BitTorrent или P2P-трафик.' },
        { kind: 'GEOSITE', category: 'category-entertainment', label: 'Видео и развлечения — широкая категория' },
        { kind: 'GEOSITE', category: 'category-games', label: 'Игры — широкая категория' },
        { kind: 'GEOSITE', category: 'speedtest', label: 'Speedtest' },
        { kind: 'GEOSITE', category: 'openai', label: 'OpenAI / ChatGPT' },
        { kind: 'GEOIP', category: 'US', label: 'США (IP)' },
      ],
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={client}><RulesPage /></QueryClientProvider>)

    expect(await screen.findByRole('heading', { name: 'P2P / торренты' })).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Видео и стриминг' })).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Игры и загрузки' })).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Инфраструктура и специальные категории' })).toBeVisible()
    expect(screen.getByText(/Сопоставляется по домену трекера/)).toBeVisible()
    expect(create).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: /Публичные торрент-трекеры/ }))
    expect(create).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Авто VPN WG-IMP → HY2-NL' }))
    fireEvent.click(screen.getByRole('button', { name: 'Добавить правило' }))

    await waitFor(() => expect(create).toHaveBeenCalled())
    expect(create.mock.calls[0]?.[0]).toEqual({ kind: 'GEOSITE', category: 'category-public-tracker', action: 'VPS-FALLBACK', enabled: true })
  })
})
