import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { RoutesPage, routeFixture } from './RoutesPage'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('RoutesPage', () => {
  it('оставляет роли неизменными, когда контроллер выбрал HY2 как активный резерв', () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><RoutesPage data={routeFixture({ active: 'HY2-USA' })} /></QueryClientProvider>)

    expect(screen.getByText('Основной: WG-IMP')).toBeVisible()
    expect(screen.getByText('Резервный: HY2-USA')).toBeVisible()
    expect(screen.getByText('сейчас: HY2-USA')).toBeVisible()
    expect(screen.getByText('переключён на резерв')).toBeVisible()
    expect(screen.queryByText('WG-IMP → HY2-USA')).not.toBeInTheDocument()
  })

  it('показывает две последние проверки, раскрывает историю и запускает проверку вручную', async () => {
    vi.spyOn(api, 'routes').mockResolvedValue({
      groups: [{ name: 'AUTO', kind: 'Fallback', choices: ['WG-IMP', 'HY2-USA'], selected: 'WG-IMP' }],
      fallback: { primary: 'WG-IMP', reserve: 'HY2-USA', selected: 'WG-IMP' },
      last_switch: null,
      probes: [
        { observed_at: '2026-07-14T10:02:00Z', target: 'WG-IMP', succeeded: false, latency_ms: null, endpoint: 'https://three.example/', outbound: 'WG-IMP', status: 'failed', status_code: null, reason: 'controller request timed out' },
        { observed_at: '2026-07-14T10:01:00Z', target: 'WG-IMP', succeeded: true, latency_ms: 42, endpoint: 'https://two.example/', outbound: 'WG-IMP', status: 'ok', status_code: null, reason: null },
        { observed_at: '2026-07-14T10:00:00Z', target: 'WG-IMP', succeeded: true, latency_ms: 41, endpoint: 'https://one.example/', outbound: 'WG-IMP', status: 'ok', status_code: null, reason: null },
      ],
    })
    vi.spyOn(api, 'probeTargets').mockResolvedValue([])
    const run = vi.spyOn(api, 'runProbes').mockResolvedValue()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={client}><RoutesPage /></QueryClientProvider>)

    expect(await screen.findByText('https://three.example/')).toBeVisible()
    expect(screen.getByText('https://two.example/')).toBeVisible()
    expect(screen.getByText('https://one.example/')).not.toBeVisible()
    fireEvent.click(screen.getByText('Показать ещё 1'))
    expect(screen.getByText('https://one.example/')).toBeVisible()
    expect(screen.getByText('controller request timed out')).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: 'Проверить сейчас' }))
    await waitFor(() => expect(run).toHaveBeenCalledOnce())
    expect(screen.getByText('Проверка запущена. Результаты появятся в журнале после завершения.')).toBeVisible()
  })

  it('показывает поэтапную диагностику выбранного зарубежного сайта через конкретный выход', async () => {
    vi.spyOn(api, 'routes').mockResolvedValue({
      groups: [{ name: 'AUTO', kind: 'Fallback', choices: ['WG-IMP', 'HY2-USA'], selected: 'WG-IMP' }],
      fallback: { primary: 'WG-IMP', reserve: 'HY2-USA', selected: 'WG-IMP' }, last_switch: null, probes: [],
    })
    vi.spyOn(api, 'probeTargets').mockResolvedValue([
      { key: 'openai', label: 'OpenAI', url: 'https://www.openai.com/', enabled: true, position: 1, is_custom: false },
    ])
    vi.spyOn(api, 'diagnoseProbe').mockResolvedValue({
      observed_at: '2026-07-14T12:00:00Z', outbound: 'WG-IMP', endpoint: 'https://www.openai.com/', conclusion: 'exit_failure',
      conclusion_text: 'Контроллер и DNS отвечают; проверка через выбранный выход не завершилась.',
      controller: { succeeded: true, latency_ms: 4, reason: null },
      dns: { succeeded: true, latency_ms: 9, reason: null, hostname: 'www.openai.com', addresses: ['104.18.33.45'] },
      exit: { succeeded: false, latency_ms: null, reason: 'controller request timed out' },
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={client}><RoutesPage /></QueryClientProvider>)

    await screen.findByText('Сайты для проверок')
    fireEvent.click(screen.getByRole('button', { name: 'Диагностировать OpenAI через WG-IMP' }))
    expect(await screen.findByText('Контроллер и DNS отвечают; проверка через выбранный выход не завершилась.')).toBeVisible()
    expect(screen.getByText('DNS Mihomo: www.openai.com → 104.18.33.45')).toBeVisible()
    expect(screen.getByText('Выход WG-IMP: controller request timed out')).toBeVisible()
  })

  it('создаёт собственную цель проверки без перезагрузки страницы', async () => {
    vi.spyOn(api, 'routes').mockResolvedValue({
      groups: [], fallback: { primary: 'WG-IMP', reserve: 'HY2-USA', selected: 'WG-IMP' }, probes: [], last_switch: null,
    })
    vi.spyOn(api, 'probeTargets').mockResolvedValue([])
    const create = vi.spyOn(api, 'createProbeTarget').mockResolvedValue({
      key: 'custom:abc', label: 'Мой сайт', url: 'https://example.com/health', enabled: true, position: 80, is_custom: true,
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(<QueryClientProvider client={client}><RoutesPage /></QueryClientProvider>)

    await screen.findByText('Сайты для проверок')
    fireEvent.change(screen.getByLabelText('Название сайта'), { target: { value: 'Мой сайт' } })
    fireEvent.change(screen.getByLabelText('HTTPS-адрес'), { target: { value: 'https://example.com/health' } })
    fireEvent.click(screen.getByRole('button', { name: 'Добавить сайт' }))

    await waitFor(() => expect(create.mock.calls[0]?.[0]).toEqual({ label: 'Мой сайт', url: 'https://example.com/health' }))
    expect(await screen.findByText('Мой сайт')).toBeVisible()
  })
})
