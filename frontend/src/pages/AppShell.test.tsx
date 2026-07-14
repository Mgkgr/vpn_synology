import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { MemoryRouter } from 'react-router-dom'

import { AppShell } from '../components/AppShell'

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
})
