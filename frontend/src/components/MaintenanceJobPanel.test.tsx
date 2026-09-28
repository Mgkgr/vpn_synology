import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { MaintenanceJobPanel } from './MaintenanceJobPanel'

afterEach(cleanup)
it('не считает потерянный ответ успешным и не предлагает повтор', () => {
  render(<MaintenanceJobPanel job={{ job_id: 'b'.repeat(32), phase: 'unknown', cancel_allowed: false }} />)
  expect(screen.getByText('Результат пока не подтверждён')).toBeVisible()
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
})
it('показывает разрешённую отмену и времена, не сырой текст исключения', () => {
  const cancel = vi.fn()
  render(<MaintenanceJobPanel job={{ job_id: 'b'.repeat(32), phase: 'backup', component: 'mihomo', started_at: 1789990000, cancel_allowed: true, error_code: 'secret=raw' }} onCancel={cancel} />)
  expect(screen.getByText('Зашифрованная резервная копия')).toBeVisible()
  expect(screen.queryByText('secret=raw')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Отменить задание' }))
  expect(cancel).toHaveBeenCalledOnce()
})
