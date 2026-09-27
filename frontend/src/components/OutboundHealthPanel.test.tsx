import { act, cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { OutboundHealthPanel } from './OutboundHealthPanel'
import type { OutboundHealthResponse } from '../api/types'

const now = new Date('2026-09-27T08:20:00Z').valueOf()
function fixture(): OutboundHealthResponse {
  return {
    enabled: true, observed_at: new Date(now).toISOString(), collector_state: 'healthy',
    outbounds: [
      { id: 'WG-IMP', label: 'VLESS-NL', engine: 'vless', state: 'pending', observed_at: new Date(now).toISOString(), pending_since: new Date(now - 4 * 60_000).toISOString(), incident_id: null, incident_started_at: null, last_success_at: null, recovery_streak: 0, successes: 1, total: 3, reasons: [], selected_fallback: 'HY2-USA' },
      { id: 'HY2-USA', label: 'HY2-DE', engine: 'hysteria2', state: 'down', observed_at: new Date(now).toISOString(), pending_since: null, incident_id: 1, incident_started_at: new Date(now - 12 * 60_000).toISOString(), last_success_at: null, recovery_streak: 0, successes: 0, total: 3, reasons: [], selected_fallback: 'HY2-USA' },
    ],
    delivery: { state: 'accepted', error_code: null, telegram_delivery: 'unconfirmed', monitors: [{ key: 'collector', state: 'accepted', last_attempt_at: new Date(now).toISOString(), last_accepted_at: new Date(now).toISOString(), error_code: null }] },
  }
}

afterEach(() => { cleanup(); vi.useRealTimers() })

describe('OutboundHealthPanel', () => {
  it('shows measured failure time, confirmation and honest delivery status', () => {
    vi.useFakeTimers(); vi.setSystemTime(now)
    render(<OutboundHealthPanel data={fixture()} isLoading={false} error={null} />)
    expect(screen.getByText('Проверяем 4 мин')).toBeVisible()
    expect(screen.getByText(/Недоступен с .*12 мин/)).toBeVisible()
    expect(screen.getByText(/Kuma приняла/)).toBeVisible()
    expect(screen.getByText(/Доставка в Telegram не подтверждена/)).toBeVisible()
    expect(screen.queryByText('ANTIDPI')).not.toBeInTheDocument()
    act(() => vi.advanceTimersByTime(60_000))
    expect(screen.getByText('Проверяем 5 мин')).toBeVisible()
    act(() => vi.advanceTimersByTime(135_000))
    expect(screen.getAllByText(/Нет данных · последняя проверка/)).toHaveLength(2)
    expect(screen.getByText(/Инцидент остаётся открытым/)).toBeVisible()
  })

  it('renders loading, unavailable and a registered third route without green unknown', () => {
    vi.useFakeTimers(); vi.setSystemTime(now)
    const view = render(<OutboundHealthPanel isLoading error={null} />)
    expect(screen.getByRole('status')).toHaveTextContent('Загрузка состояния выходов')
    view.rerender(<OutboundHealthPanel isLoading={false} error={new Error('offline')} />)
    expect(screen.getByRole('alert')).toHaveTextContent('Состояние выходов не получено')
    const data = fixture()
    data.outbounds.push({ ...data.outbounds[0], id: 'ANTIDPI', label: 'ANTIDPI', engine: 'zapret2', state: 'unknown', pending_since: null })
    view.rerender(<OutboundHealthPanel data={data} isLoading={false} error={null} />)
    const card = screen.getByRole('article', { name: 'ANTIDPI' })
    expect(within(card).queryByText('В норме')).not.toBeInTheDocument()
    expect(within(card).getByText(/Нет данных/)).toBeVisible()
  })
})
