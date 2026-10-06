import { afterEach, describe, expect, it, vi } from 'vitest'

import { api, setUnauthorizedHandler } from './client'

afterEach(() => {
  api.clearSession()
  vi.unstubAllGlobals()
})

describe('клиент защищённого API', () => {
  it('reads service snapshots using GET without a diagnostic mutation', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ enabled: false, services: [] }), { status: 200 }))
    vi.stubGlobal('fetch', fetcher)
    await api.siteProbes()
    expect(fetcher).toHaveBeenCalledOnce()
    const [url, options] = fetcher.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/rules/service-checks')
    expect(options.method ?? 'GET').toBe('GET')
    expect(options.body).toBeUndefined()
    expect(options.signal).toBeDefined()
  })
  it('читает job id из принятого 202 ответа обслуживания', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ job_id: 'b'.repeat(32), phase: 'queued' }), { status: 202 })))
    const response = await api.submitMaintenance('b'.repeat(32), {
      action: 'restart', components: ['mihomo'], expected_revision: 'a'.repeat(64),
      release_ids: {}, enable_stopped: [], snapshot_id: null, accept_data_loss: false,
    }, 'grant')
    expect(response.job_id).toBe('b'.repeat(32))
  })
  it('сбрасывает локальное состояние и уведомляет приложение после 401', async () => {
    const onUnauthorized = vi.fn()
    const removeHandler = setUnauthorizedHandler(onUnauthorized)
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 401 })))

    await expect(api.overview()).rejects.toMatchObject({ status: 401 })

    expect(onUnauthorized).toHaveBeenCalledTimes(1)
    removeHandler()
  })

  it('объясняет отсутствие первого снимка NAS вместо общего сообщения об ошибке', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 503 })))

    await expect(api.hostHealth()).rejects.toMatchObject({
      status: 503,
      message: expect.stringContaining('Снимок состояния NAS ещё не собран'),
    })
  })

  it('explains a DIRECT revision conflict without suggesting an immediate retry', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 409 })))
    await expect(api.applyDirectRules('DOMAIN,example.org,DIRECT', 'a'.repeat(64))).rejects.toMatchObject({
      status: 409,
      message: 'Изменение DIRECT-списка отклонено: конфликт состояния или другая операция. Черновик сохранён; обновите сведения перед повтором.',
    })
  })
})
