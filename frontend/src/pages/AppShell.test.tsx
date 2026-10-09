import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { MemoryRouter } from 'react-router-dom'

import { AppShell } from '../components/AppShell'

afterEach(cleanup)

describe('AppShell', () => {
  it('показывает русскую навигацию для всех рабочих страниц', () => {
    render(<MemoryRouter><AppShell><main>Содержимое</main></AppShell></MemoryRouter>)

    expect(screen.getByRole('link', { name: 'Обзор' })).toBeVisible()
    expect(screen.getByRole('link', { name: 'Маршруты' })).toBeVisible()
    expect(screen.getByRole('link', { name: 'Клиенты' })).toBeVisible()
    expect(screen.getByRole('link', { name: 'Правила' })).toBeVisible()
    expect(screen.getByRole('link', { name: 'Обновления' })).toBeVisible()
    expect(screen.getByRole('link', { name: 'Журнал' })).toBeVisible()
  })

  it('opens the dedicated DPI section and highlights it instead of Routes', () => {
    render(<MemoryRouter initialEntries={['/routes']}><AppShell><main>Содержимое</main></AppShell></MemoryRouter>)

    const link = screen.getByRole('link', { name: 'Обход DPI' })
    expect(link).toHaveAttribute('href', '/routes/antidpi/strategies')
    fireEvent.click(link)
    expect(link).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: 'Маршруты' })).not.toHaveAttribute('aria-current')
    expect(screen.getByRole('link', { name: 'Правила' })).toHaveTextContent('04')
  })

  it('keeps Routes selected while editing VPN keys', () => {
    render(<MemoryRouter initialEntries={['/routes/profiles']}><AppShell /></MemoryRouter>)
    expect(screen.getByRole('link', { name: 'Маршруты' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: 'Обход DPI' })).not.toHaveAttribute('aria-current')
  })
})
