import type { ServiceProbe, ServiceProbeResponse, ServiceProbeRoute } from '../../api/types'
import { formatDate } from '../../components/Status'

const routeNames = { direct: 'DIRECT', primary: 'Основной', reserve: 'Резерв' }
const reasons: Record<string, string> = {
  not_checked: 'Ещё не проверено.',
  probe_group_invalid: 'Изолированная группа отсутствует или не прошла проверку.',
  probe_mode_invalid: 'Режим одного HEAD не подтверждён: unified-delay должен быть выключен. Рабочие настройки не изменялись.',
  controller_unavailable: 'Контроллер недоступен; доступность выхода не установлена.',
  dns_preflight_failed: 'DNS-проверка не пройдена: адреса отсутствуют, непубличные либо ответ некорректен. HEAD не отправлен.',
  probe_timeout: 'Контрольный запрос не завершился за 10 секунд; контроллер доступен.',
  probe_failed: 'Запрос не завершился; контроллер доступен. Точную причину API не сообщает.',
  probe_result_unconfirmed: 'Свежий результат контрольного запроса не подтверждён.',
  http_status_outside_expected: 'Вне 2xx/3xx; точный код API не сообщает. Это не означает отказ VPN.',
  collector_error: 'Сбор не завершён; состояние сайта не установлено.',
  collector_timeout: 'Превышено время сбора; состояние сайта не установлено.',
}

function resultText(route: ServiceProbeRoute) {
  if (route.state === 'responded') return `${route.delay_ms} мс`
  if (route.state === 'http_rejected') return `HTTP-ответ · ${route.delay_ms} мс`
  if (route.state === 'failed') return route.reason === 'probe_timeout' ? 'таймаут' : 'нет ответа'
  return 'нет данных'
}

export function ServiceProbeSummary({ data, loading = false, error = false }: { data?: ServiceProbeResponse; loading?: boolean; error?: boolean }) {
  return <section className="service-probe-summary" aria-label="Суточные замеры сервисов">
    <strong>Контрольные сайты · HTTPS HEAD</strong>
    {loading ? <p role="status">Читаем сохранённые замеры…</p>
      : error ? <p role="status">Суточные замеры недоступны. Назначения правил доступны; предыдущие результаты не подтверждены.</p>
      : !data?.enabled ? <p>Суточные замеры выключены. Включение — после проверки изоляции на NAS.</p>
      : <>
        <p>Ежедневно в 04:30 · {data.timezone}. {data.next_run_at && <>Следующий прогон: <time dateTime={data.next_run_at}>{formatDate(data.next_run_at)}</time> (время браузера).</>}</p>
        {data.run ? <p>{data.run.state === 'running' ? 'Прогон выполняется' : data.run.state === 'interrupted' ? 'Прогон прерван' : 'Прогон завершён'}: {data.run.completed_count} из {data.run.expected_count} · {data.run.day}. Завершение сбора не означает успешный ответ всех сайтов.</p> : <p>Первый прогон ещё не начат.</p>}
      </>}
    <p>Замеры не меняют маршруты и fallback. Обновление сведений читает только сохранённые результаты.</p>
  </section>
}

export function ServiceProbeStatus({ service, enabled = false, expanded = false, loading = false, error = false }: {
  service?: ServiceProbe; enabled?: boolean; expanded?: boolean; loading?: boolean; error?: boolean
}) {
  if (loading && !service) return <div className="service-probe-status">Замеры загружаются…</div>
  if (!service) return <div className="service-probe-status">{error ? 'Снимок замеров недоступен' : 'Для категории нет единого контрольного сайта'}</div>
  const times = service.routes.flatMap((route) => route.observed_at ? [route.observed_at] : [])
  const latest = times.sort((a, b) => Date.parse(b) - Date.parse(a))[0]
  return <div className="service-probe-status" aria-label="Замеры контрольного сайта">
    <div className="service-probe-chips">{service.routes.map((route) => {
      const historic = error || !enabled || route.stale || !route.current_run
      const text = error ? 'не подтверждено' : !route.observed_at ? 'ещё не проверено' : !enabled ? 'архив' : route.stale ? 'устарело' : !route.current_run ? 'прошлый прогон' : resultText(route)
      const tone = historic || route.state === 'unknown' ? 'muted' : route.state === 'responded' ? 'good' : route.state === 'http_rejected' ? 'warning' : 'bad'
      return <span key={route.route_id} data-probe-tone={tone} title={`${route.label}: ${text}`}>{routeNames[route.route_id]} · {text}</span>
    })}</div>
    <small>Контрольный сайт{latest && <> · последний замер <time dateTime={latest}>{formatDate(latest)}</time></>}</small>
    {expanded && <div className="service-probe-details">
      <p>HEAD не проверяет вход, видео или работу всего приложения. Миллисекунды — время ответа HTTPS, не ICMP ping. Редиректы не выполняются.</p>
      <dl>{service.routes.map((route) => <div key={route.route_id}>
        <dt>{route.label}</dt>
        <dd>{route.observed_at ? <><time dateTime={route.observed_at}>{formatDate(route.observed_at)}</time> · {resultText(route)}</> : 'Ещё не проверено'}{route.stale ? ' · данные устарели' : route.observed_at && !route.current_run ? ' · из прошлого прогона' : ''}</dd>
        <dd className="service-probe-url">{route.url}</dd>
        {route.reason && <dd>{reasons[route.reason] ?? 'Причина не уточняется API.'}</dd>}
      </div>)}</dl>
    </div>}
  </div>
}
