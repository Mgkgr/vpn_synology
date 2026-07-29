import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import type { RealtimeTrafficPeriod } from '../api/types'
import { AsyncState } from '../components/AsyncState'
import { formatBytes, formatDate, formatRate, Status } from '../components/Status'
import { TrafficChart } from '../components/TrafficChart'

const ACTIVE_HANDSHAKE_WINDOW_MS = 5 * 60_000

type OverviewClient = {
  id: number
  name: string
  ipv4_address: string
  enabled: boolean
  latest_handshake_at: string | null
  received_bytes: number
  transmitted_bytes: number
}

export function OverviewPage() {
  const [trafficPeriod, setTrafficPeriod] = useState<RealtimeTrafficPeriod>('30m')
  const overview = useQuery({
    queryKey: ['overview'],
    queryFn: api.overview,
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
  })
  const realtimeTraffic = useQuery({
    queryKey: ['traffic-realtime', trafficPeriod],
    queryFn: () => api.realtimeTraffic(trafficPeriod),
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
  })
  const updates = useQuery({ queryKey: ['updates'], queryFn: api.updates })
  const journal = useQuery({ queryKey: ['journal', 'latest'], queryFn: () => api.journal() })
  const hostHealth = useQuery({
    queryKey: ['host-health'],
    queryFn: api.hostHealth,
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
  })
  const data = overview.data
  const traffic = realtimeTraffic.data?.points.at(-1) ?? null
  const selectedExit = data?.fallback.selected ?? null
  const activeClients = (data?.clients ?? []).filter(isActiveWireGuardClient)

  return (
    <main className="page overview-page">
      <header className="page-header">
        <div><p className="eyebrow">01 / СОСТОЯНИЕ СИСТЕМЫ</p><h1>Обзор</h1><p>Оперативная сводка защищённого шлюза.</p></div>
        <div className="header-status"><Status ok={selectedExit ? true : null} pending={!selectedExit} /><span>{selectedExit ? `выбран ${selectedExit}` : 'нет данных о выходе'}</span></div>
      </header>
      <AsyncState loading={overview.isLoading} error={overview.error}>
        <section className="services-section" aria-labelledby="services-title">
          <div className="section-heading"><h2 id="services-title">Сервисы</h2><span>проверка доступности</span></div>
          <AsyncState loading={overview.isLoading} error={overview.error} empty={!data?.services.length} emptyLabel="Нет данных.">{data?.services && <table className="data-table"><thead><tr><th>Сервис</th><th>Состояние</th><th>Сведения</th></tr></thead><tbody>{data.services.map((service) => <tr key={service.name}><th scope="row">{service.name}</th><td><Status ok={service.succeeded} /></td><td>{service.status ?? 'нет данных'}{service.latency_ms !== null ? ` · ${service.latency_ms} мс` : ''}</td></tr>)}</tbody></table>}</AsyncState>
        </section>
        <section className="panel host-health-panel" aria-labelledby="host-health-title">
          <div className="section-heading"><div><h2 id="host-health-title">Хост DS923+</h2><span>снимок раз в минуту · {hostHealth.data ? formatDate(hostHealth.data.observed_at) : 'ожидание данных'}</span></div><Status ok={hostHealth.data ? true : null} pending={hostHealth.isLoading} /></div>
          <AsyncState loading={hostHealth.isLoading} error={hostHealth.error} empty={!hostHealth.data} emptyLabel="Снимок состояния NAS ещё не собран.">
            {hostHealth.data && <div className="host-health-grid">
              <dl className="metric-list"><div><dt>CPU</dt><dd>{hostHealth.data.cpu_usage_percent === null ? 'первая точка' : `${hostHealth.data.cpu_usage_percent.toFixed(1)}%`}</dd></div><div><dt>Нагрузка</dt><dd>{hostHealth.data.load_one.toFixed(2)} / {hostHealth.data.load_five.toFixed(2)} / {hostHealth.data.load_fifteen.toFixed(2)}</dd></div><div><dt>Память свободна</dt><dd>{formatBytes(hostHealth.data.memory_available_bytes)} из {formatBytes(hostHealth.data.memory_total_bytes)}</dd></div><div><dt>Swap свободен</dt><dd>{formatBytes(hostHealth.data.swap_free_bytes)} из {formatBytes(hostHealth.data.swap_total_bytes)}</dd></div><div><dt>Том свободен</dt><dd>{formatBytes(hostHealth.data.volume_available_bytes)} из {formatBytes(hostHealth.data.volume_total_bytes)}</dd></div><div><dt>Ошибки сети</dt><dd>{hostHealth.data.network_rx_errors + hostHealth.data.network_tx_errors} · потери {hostHealth.data.network_rx_dropped + hostHealth.data.network_tx_dropped}</dd></div></dl>
              <table className="data-table compact-host-table"><thead><tr><th>Контейнер</th><th>Состояние</th><th>Перезапуски</th></tr></thead><tbody>{hostHealth.data.containers.map((container) => <tr key={container.name}><th scope="row">{container.name}</th><td><Status ok={container.state === 'running' && (container.health === null || container.health === 'healthy')} /></td><td>{container.restart_count}</td></tr>)}</tbody></table>
            </div>}
          </AsyncState>
        </section>
        <section className="exit-rows" aria-label="Выходы fallback-группы">
          <article className="exit-row primary"><div><p className="eyebrow">ВЫХОД</p><h2>Основной: WG-IMP</h2><p>{selectedExit === 'WG-IMP' ? 'выбран fallback-группой.' : 'не выбран fallback-группой.'}</p></div><Status ok={selectedExit === 'WG-IMP' ? true : null} pending={selectedExit !== 'WG-IMP'} /></article>
          <article className="exit-row reserve"><div><p className="eyebrow">ВЫХОД</p><h2>Резервный: HY2-NL</h2><p>{selectedExit === 'HY2-NL' ? 'переключён на резерв' : 'не выбран fallback-группой.'}</p></div><Status ok={selectedExit === 'HY2-NL' ? true : null} pending={selectedExit !== 'HY2-NL'} /></article>
        </section>
        <div className="overview-grid">
          <section className="panel traffic-panel" aria-labelledby="traffic-title">
            <div className="section-heading"><div><h2 id="traffic-title">Трафик</h2><span>{traffic ? `скорость канала · получено ${formatDate(traffic.observed_at)}` : 'скорость канала · сбор раз в минуту'}</span></div><div className="period-toggle" role="group" aria-label="Период графика скорости"><button className={trafficPeriod === '5m' ? 'active' : ''} onClick={() => setTrafficPeriod('5m')} type="button">5 минут</button><button className={trafficPeriod === '30m' ? 'active' : ''} onClick={() => setTrafficPeriod('30m')} type="button">30 минут</button><button className={trafficPeriod === '6h' ? 'active' : ''} onClick={() => setTrafficPeriod('6h')} type="button">6 часов</button></div></div>
            <div className="traffic-totals"><div><span>Входящий</span><strong>{formatRate(traffic?.down_bps ?? 0)}</strong></div><div><span>Исходящий</span><strong>{formatRate(traffic?.up_bps ?? 0)}</strong></div></div>
            <AsyncState loading={realtimeTraffic.isLoading} error={realtimeTraffic.error}>
              <TrafficChart points={realtimeTraffic.data?.points ?? []} />
            </AsyncState>
          </section>
          <section className="panel geo-panel" aria-labelledby="geo-title">
            <div className="section-heading"><h2 id="geo-title">GeoIP / GeoSite</h2></div>
            <AsyncState loading={updates.isLoading} error={updates.error} empty={!updates.data?.updates.length} emptyLabel="Обновления геоданных ещё не зафиксированы.">
              {updates.data?.updates[0] && <dl className="metric-list"><div><dt>Последняя операция</dt><dd>{updates.data.updates[0].operation ?? 'Geo update'}</dd></div><div><dt>Дата</dt><dd>{formatDate(updates.data.updates[0].observed_at)}</dd></div><div><dt>Проверка</dt><dd>{overviewGeoResult(updates.data.updates[0])}</dd></div><div><dt>Результат</dt><dd><Status ok={updates.data.updates[0].succeeded} pending={updates.data.updates[0].succeeded === null} /></dd></div></dl>}
            </AsyncState>
          </section>
        </div>
        <section className="panel" aria-labelledby="clients-title">
          <div className="section-heading"><h2 id="clients-title">Активные клиенты</h2><span>Активно: {activeClients.length} из {data?.client_count ?? 0} · handshake ≤ 5 мин</span></div>
          <ClientTable clients={activeClients} />
        </section>
        <section className="panel" aria-labelledby="events-title">
          <div className="section-heading"><h2 id="events-title">Последние события</h2><span>аудит панели</span></div>
          <AsyncState loading={journal.isLoading} error={journal.error} empty={!journal.data?.events.length} emptyLabel="Событий пока нет.">
            {journal.data && <table className="data-table"><thead><tr><th>Время</th><th>Событие</th><th>Инициатор</th><th>Результат</th></tr></thead><tbody>{journal.data.events.slice(0, 6).map((event) => <tr key={event.id}><td>{formatDate(event.observed_at)}</td><th scope="row">{event.action}</th><td>{event.actor}</td><td><Status ok={event.succeeded} /></td></tr>)}</tbody></table>}
          </AsyncState>
        </section>
      </AsyncState>
    </main>
  )
}

function overviewGeoResult(update: { verification: string; changed_files: string[]; checked_files: string[] }): string {
  if (update.verification === 'changed') return `изменены: ${update.changed_files.join(', ')}`
  if (update.verification === 'unchanged') return 'файлы уже актуальны'
  if (update.verification === 'unavailable') return 'файлы не удалось проверить'
  if (update.verification === 'pending') return 'выполняется'
  return update.verification === 'failed' ? 'ошибка, прежние файлы сохранены' : 'снимок файлов сохранён'
}

function isActiveWireGuardClient(client: OverviewClient): boolean {
  if (!client.enabled || !client.latest_handshake_at) return false
  const handshakeAt = Date.parse(client.latest_handshake_at)
  if (!Number.isFinite(handshakeAt)) return false
  const ageMs = Date.now() - handshakeAt
  return ageMs >= -60_000 && ageMs <= ACTIVE_HANDSHAKE_WINDOW_MS
}

function ClientTable({ clients }: { clients: OverviewClient[] }) {
  if (!clients.length) return <p className="state-message">Активных клиентов пока нет.</p>
  return <table className="data-table"><thead><tr><th>Клиент</th><th>Адрес</th><th>Handshake</th><th>Трафик</th><th>Состояние</th></tr></thead><tbody>{clients.map((client) => <tr key={client.id}><th scope="row">{client.name}</th><td>{client.ipv4_address}</td><td>{formatDate(client.latest_handshake_at)}</td><td>{formatBytes(client.received_bytes + client.transmitted_bytes)}</td><td><span className="status-label healthy"><span className="status-dot healthy" aria-hidden="true" />подключён</span></td></tr>)}</tbody></table>
}
