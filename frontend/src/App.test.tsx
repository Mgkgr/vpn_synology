import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'

import { api } from './api/client'
import { App } from './App'

const authSession = { csrf_token: 'csrf-token' }

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  cleanup()
  api.clearSession()
  vi.unstubAllGlobals()
})

describe('authenticated application routes', () => {
  it('returns the restored auth session', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(authSession)))

    await expect(api.restoreSession()).resolves.toEqual(authSession)
  })

  it('renders a protected route after session restoration succeeds', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      if (String(input) === '/api/auth/csrf') return jsonResponse(authSession)
      if (String(input) === '/api/routes') {
        return jsonResponse({
          groups: [{ name: 'AUTO', kind: 'Selector', choices: ['WG-IMP', 'HY2-USA'], selected: 'WG-IMP' }],
          fallback: { primary: 'WG-IMP', reserve: 'HY2-USA', selected: 'WG-IMP' },
          probes: [],
          last_switch: null,
        })
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    }))

    render(<MemoryRouter initialEntries={['/routes']}><App /></MemoryRouter>)

    expect(await screen.findByRole('button', { name: /VLESS-NL/ })).toBeVisible()
  })

  it('redirects an unauthenticated initial session check to the login page', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 401 })))

    render(<MemoryRouter initialEntries={['/overview']}><App /></MemoryRouter>)

    expect(await screen.findByRole('button', { name: /Войти/i })).toBeVisible()
  })
})
