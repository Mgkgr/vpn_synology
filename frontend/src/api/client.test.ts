import { afterEach, describe, expect, it, vi } from 'vitest'

import { api, setUnauthorizedHandler } from './client'

afterEach(() => {
  api.clearSession()
  vi.unstubAllGlobals()
})

describe('клиент защищённого API', () => {
  it('сбрасывает локальное состояние и уведомляет приложение после 401', async () => {
    const onUnauthorized = vi.fn()
    const removeHandler = setUnauthorizedHandler(onUnauthorized)
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 401 })))

    await expect(api.overview()).rejects.toMatchObject({ status: 401 })

    expect(onUnauthorized).toHaveBeenCalledTimes(1)
    removeHandler()
  })
})
