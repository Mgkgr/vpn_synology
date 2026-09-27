import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import type { OutboundHealthItem, OutboundHealthResponse } from '../api/types'
import { formatDate, formatElapsed } from './Status'
import './OutboundHealthPanel.css'

export function useOutboundHealth(enabled = true) {
  return useQuery({ queryKey: ['outbound-health'], queryFn: api.outboundHealth, enabled, staleTime: 30_000, refetchInterval: 60_000, refetchIntervalInBackground: false })
}

function useVisibleClock() {
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | undefined
    const refresh = () => {
      if (timer) clearInterval(timer)
      timer = undefined
      if (document.visibilityState !== 'hidden') {
        setNow(Date.now())
        timer = setInterval(() => setNow(Date.now()), 15_000)
      }
    }
    refresh()
    document.addEventListener('visibilitychange', refresh)
    return () => { if (timer) clearInterval(timer); document.removeEventListener('visibilitychange', refresh) }
  }, [])
  return now
}

export function OutboundHealthPanel({ data, isLoading, error }: { data?: OutboundHealthResponse; isLoading: boolean; error: Error | null }) {
  const now = useVisibleClock()
  if (isLoading && !data) return <section className="panel outbound-health-panel"><p className="state-message" role="status">Загрузка состояния выходов…</p></section>
  if (!data) return <section className="panel outbound-health-panel"><p className="state-message" role="alert">Состояние выходов не получено. Проверьте связь с панелью.</p></section>
  if (!data.enabled) return <section className="panel outbound-health-panel"><p className="state-message">Независимый мониторинг выходов ещё не включён. Ниже — прежние диагностические проверки.</p></section>
  const delivery = data.delivery
  return <section className="panel outbound-health-panel" aria-label="Здоровье выходов">
    <div className="section-heading"><div><h2>Здоровье выходов</h2><p>3 контрольных адреса · отказ подтверждается после 10 мин · восстановление после 2 циклов</p></div></div>
    {error && <p className="form-error" role="alert">Не удалось обновить данные. Показан последний сохранённый снимок.</p>}
    <div className="outbound-health-grid">{data.outbounds.map((item) => <HealthCard key={item.id} item={item} now={now} />)}</div>
    <p className="outbound-health-delivery">Доставка в Telegram не подтверждена панелью. Получение сообщения проверяется отдельно.</p>
    <details className="outbound-health-delivery"><summary>Уведомления: {({ disabled: 'не включены', pending: 'ожидают отправки', accepted: 'Kuma приняла', error: 'ошибка отправки' })[delivery.state]}</summary>
      {delivery.error_code && <p>Код: {delivery.error_code}</p>}
      {delivery.monitors.map((monitor) => <p key={monitor.key}>{data.outbounds.find((item) => item.id === monitor.key)?.label ?? 'Мониторинг'} · попытка: {formatDate(monitor.last_attempt_at)} · принято Kuma: {formatDate(monitor.last_accepted_at)}{monitor.error_code ? ` · ${monitor.error_code}` : ''}</p>)}
    </details>
  </section>
}

function HealthCard({ item, now }: { item: OutboundHealthItem; now: number }) {
  const age = item.observed_at ? now - Date.parse(item.observed_at) : NaN
  const state = !Number.isFinite(age) || age > 180_000 || age < -5_000 ? 'unknown' : item.state
  const text = state === 'unknown' ? `Нет данных · последняя проверка ${formatDate(item.observed_at)}`
    : state === 'pending' ? `Проверяем ${formatElapsed(item.pending_since, now)}`
      : state === 'down' ? `Недоступен с ${formatDate(item.incident_started_at)} · ${formatElapsed(item.incident_started_at, now)}`
        : state === 'degraded' ? 'Частичный сбой: 2 из 3 проверок успешны' : 'В норме'
  return <article aria-label={item.label} className={`outbound-health-card health-${state}`}>
    <h3>{item.label}</h3><p className="health-observation">{text}</p>
    <p>{item.engine} · {state === 'unknown' ? 'достоверных свежих проб нет' : `${item.successes} из ${item.total} проверок успешны`}</p>
    {state !== 'unknown' && <p>Проверено: {formatDate(item.observed_at)}</p>}
    {state === 'unknown' && item.incident_id !== null && <p>Инцидент остаётся открытым с {formatDate(item.incident_started_at)}.</p>}
    {item.recovery_streak > 0 && state !== 'unknown' && <p>Восстановление: {item.recovery_streak} из 2 циклов.</p>}
  </article>
}
