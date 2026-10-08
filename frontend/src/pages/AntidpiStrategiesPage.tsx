import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import type { StrategyId, StrategyMode, StrategyOperation, StrategyPolicy, StrategyService, StrategySnapshot } from '../api/antidpiTypes'
import type { CancelOperation, MaintenanceJob } from '../api/maintenanceTypes'
import { AsyncState } from '../components/AsyncState'
import { FINISHED_PHASES, MaintenanceJobPanel, maintenanceDate } from '../components/MaintenanceJobPanel'
import { StrategyConfirmation } from '../components/StrategyConfirmation'

const BLOCKERS: Record<string, string> = {
  runtime_unverified: 'Изолированный исполнитель Anti-DPI ещё не проверен.', dns_renewal_unverified: 'Обновление DNS ещё не проверено.',
  namespace_recovery_unverified: 'Восстановление сети после перезапуска ещё не проверено.', service_isolation_unverified: 'Изоляция стратегий по сервисам ещё не проверена.',
  production_input_unverified: 'Вход Anti-DPI для реальных клиентов ещё не проверен.', worker_unavailable: 'Исполнитель стратегий не подключён или недоступен.', backup_unavailable: 'Зашифрованная резервная копия недоступна.',
}
const STATES: Record<string, string> = { disabled: 'Проверки отключены', pinned: 'Закреплена · автосмена выключена', no_data: 'Автоматических проверок ещё нет',
  stale: 'Подтверждение устарело', unknown: 'Причина не подтверждена', healthy: 'HTTPS-проверка успешна', suspect: 'Сбой подтверждается', http_error: 'Сервис ответил HTTP-ошибкой',
  cooldown: 'Пауза после смены', rate_limit: 'Достигнут лимит смен', search: 'Нужен проверенный кандидат' }
const STRATEGY_ERRORS: Record<string, string> = { verified: 'Успешно', timeout: 'Тайм-аут TCP/TLS', reset: 'Соединение сброшено', tls_transport: 'Обрыв TLS',
  http_denied: 'HTTP: доступ ограничен сервисом', http_status: 'Неожиданный HTTP-ответ', dns_unavailable: 'DNS недоступен', runtime_unavailable: 'Исполнитель недоступен',
  control_failed: 'Контрольная проверка не прошла', certificate_invalid: 'Ошибка сертификата', identity_changed: 'Версия изменилась', clock_invalid: 'Неверное время данных',
  stale: 'Данные устарели', invalid_response: 'Результат не подтверждён', probe_failed: 'Проверка не завершилась' }
type Confirmation = { jobId: string; operation: StrategyOperation | CancelOperation; name: string }

export function AntidpiStrategiesPage() {
  const client = useQueryClient(), [params, setParams] = useSearchParams()
  const rawId = params.get('job') ?? '', jobId = /^[0-9a-f]{32}$/.test(rawId) ? rawId : ''
  const [visible, setVisible] = useState(!document.hidden), [confirmation, setConfirmation] = useState<Confirmation | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  useEffect(() => {
    const change = () => { setVisible(!document.hidden); if (!document.hidden) void client.invalidateQueries({ queryKey: ['antidpi'] }) }
    document.addEventListener('visibilitychange', change); return () => document.removeEventListener('visibilitychange', change)
  }, [client])
  const snapshot = useQuery({ queryKey: ['antidpi', 'strategies'], queryFn: api.antidpiStrategies, refetchInterval: visible ? 60_000 : false, refetchIntervalInBackground: false, retry: false })
  const jobs = useQuery({ queryKey: ['maintenance', 'jobs'], queryFn: api.maintenanceJobs, refetchInterval: visible ? 60_000 : false, refetchIntervalInBackground: false, retry: false })
  const job = useQuery({ queryKey: ['maintenance', 'job', jobId], queryFn: () => api.maintenanceJob(jobId), enabled: !!jobId && visible,
    refetchInterval: query => !visible || (query.state.data && FINISHED_PHASES.has(query.state.data.phase)) ? false : 5_000, refetchIntervalInBackground: false, retry: false })
  useEffect(() => {
    if (job.data && FINISHED_PHASES.has(job.data.phase)) {
      void client.invalidateQueries({ queryKey: ['antidpi'] }); void client.invalidateQueries({ queryKey: ['maintenance', 'jobs'] })
    }
  }, [job.data?.phase, client])
  const observedJob: MaintenanceJob | null = jobId ? job.data ?? { job_id: jobId, phase: 'unknown', cancel_allowed: false } : null
  const busy = jobs.isLoading || !!jobs.error || !jobs.data?.available || jobs.data.jobs.some(item => !FINISHED_PHASES.has(item.phase)) || (!!observedJob && !FINISHED_PHASES.has(observedJob.phase))
  function prepare(operation: StrategyOperation, name: string) {
    if (busy || !snapshot.data?.can_manage) return
    setConfirmation({ jobId: crypto.randomUUID().replace(/-/g, ''), operation, name })
  }
  async function confirm(password: string) {
    if (!confirmation) return
    const { jobId: id, operation } = confirmation
    const authorization = await api.authorizeMaintenance(operation, password)
    setParams({ job: id }); client.setQueryData(['maintenance', 'job', id], { job_id: id, phase: 'unknown', cancel_allowed: false })
    try {
      const result = operation.action === 'cancel' ? await api.cancelMaintenance(id, authorization.grant) : await api.submitMaintenance(id, operation, authorization.grant)
      if (result.job_id !== id) throw new Error('Unconfirmed job id')
      client.setQueryData(['maintenance', 'job', id], result); setNotice('Задание принято. Закрытие страницы не отменяет его.')
    } catch {
      setNotice('Ответ потерян или не подтверждён. Проверяем прежний ID; повторный запуск не выполняется.')
      void client.fetchQuery({ queryKey: ['maintenance', 'job', id], queryFn: () => api.maintenanceJob(id), staleTime: 0 }).catch(() => undefined)
    }
    finally { setConfirmation(null); void client.invalidateQueries({ queryKey: ['maintenance', 'jobs'] }) }
  }
  const data = snapshot.data
  return <>
    <main className="page components-page" inert={confirmation ? true : undefined}>
      <header className="page-header"><div><p className="eyebrow">02 / МАРШРУТЫ / ANTI-DPI</p><h1>Стратегии обхода</h1><p>ByeDPI · отдельная стратегия для каждого сервиса.</p></div><a href="/routes">Все маршруты</a></header>
      <p className="maintenance-hint">Проверяем только HTTPS по TCP/443 с проверкой сертификата. Успех не подтверждает видео, звонки, приложения или UDP. Основной и резервный VPN не переключаются.</p>
      {notice && <p role="status" className="maintenance-notice">{notice}</p>}
      {data?.capabilities.blockers.length ? <section className="panel"><div className="maintenance-body"><h2>Что ещё не подтверждено</h2><ul>{data.capabilities.blockers.map(item => <li key={item}>{BLOCKERS[item] ?? 'Условие безопасности не подтверждено.'}</li>)}</ul><p>Автосмена на NAS не считается включённой, пока эти проверки не завершены.</p></div></section> : null}
      {data?.available && !data.can_manage && <p className="maintenance-notice">Режим просмотра. Управление стратегиями доступно владельцу.</p>}
      <AsyncState loading={snapshot.isLoading} error={snapshot.error} empty={!data?.services.length} emptyLabel="Нет подтверждённого состояния стратегий на NAS.">
        {data && <><p className="maintenance-hint">Каталог: {data.catalog?.version ?? 'не подтверждён'}. Суточный подбор: {data.catalog?.daily_time ?? '—'} · {data.catalog?.timezone ?? '—'}. При исправной стратегии автоматического возврата нет.</p>
          <div className="component-grid">{data.services.map(service => <StrategyCard key={`${service.service_id}-${data.revision}`} service={service} snapshot={data} busy={busy} prepare={prepare} />)}</div></>}
      </AsyncState>
      {observedJob && <MaintenanceJobPanel job={observedJob} onCancel={data?.can_manage && observedJob.cancel_allowed ? () => setConfirmation({ jobId, operation: { action: 'cancel', job_id: jobId }, name: 'Текущее задание Anti-DPI' }) : undefined} />}
      {job.error && <p role="alert" className="maintenance-notice">Связь с исполнителем потеряна. Задание не запускается повторно.</p>}
    </main>
    {confirmation && <StrategyConfirmation key={confirmation.jobId + confirmation.operation.action} operation={confirmation.operation} serviceName={confirmation.name} onConfirm={confirm} onClose={() => setConfirmation(null)} />}
  </>
}

function StrategyCard({ service, snapshot, busy, prepare }: { service: StrategyService; snapshot: StrategySnapshot; busy: boolean; prepare: (operation: StrategyOperation, name: string) => void }) {
  const [selection, setSelection] = useState<StrategyId | ''>(service.strategy_id ?? '')
  const [applyMode, setApplyMode] = useState<StrategyMode>('pinned')
  const [policy, setPolicy] = useState<StrategyPolicy>({ enabled: service.enabled, mode: service.mode, interval_minutes: service.interval_minutes, daily_enabled: service.daily_enabled })
  const base = { service_id: service.service_id, expected_revision: snapshot.revision ?? '' }
  const writable = snapshot.can_manage && !!snapshot.revision && !busy
  const canCheck = writable && snapshot.capabilities.can_check, canApply = writable && snapshot.capabilities.can_apply
  const configure = (settings: StrategyPolicy) => prepare({ ...base, action: 'strategy_configure', settings }, service.name)
  const last = service.last_check
  const currentResults = service.results.filter(item => item.strategy_id === service.strategy_id)
  const failureAge = service.first_failure_at && snapshot.observed_at ? Math.max(0, Math.floor((snapshot.observed_at - service.first_failure_at) / 60)) : null
  return <section className="panel component-card" aria-label={service.name}>
    <div className="section-heading"><h2>{service.name}</h2><span>{service.mode === 'auto' && service.enabled ? 'Авто' : 'Закреплена / авто выкл.'}</span></div>
    <div className="maintenance-body"><p><strong>Текущая: {service.strategy_id ?? 'не подтверждена'}</strong></p>
      <p>{STATES[service.state] ?? 'Нет подтверждения'}{service.state === 'suspect' ? ` · ${service.failure_count}/3` : ''}</p>
      <p className="maintenance-hint">Успешных проб текущей: {currentResults.filter(item => item.verdict === 'success').length} из {currentResults.length} последних</p>
      <dl><dt>Контрольный сайт</dt><dd>{service.host}</dd><dt>Последняя попытка</dt><dd>{maintenanceDate(last?.checked_at)}{last ? ` · ${last.strategy_id ?? service.strategy_id} · ${STRATEGY_ERRORS[last.reason] ?? 'Причина не подтверждена'}${last.latency_ms != null ? ` · ${Math.round(last.latency_ms)} мс` : ''}${last.http_status ? ` · HTTP ${last.http_status}` : ''}` : ''}</dd>
        <dt>Успех автопроверки</dt><dd>{maintenanceDate(service.last_success_at)}</dd><dt>Серия отказов с</dt><dd>{maintenanceDate(service.first_failure_at)}{failureAge !== null ? ` · ${failureAge} мин.` : ''}</dd></dl>
      <div className="maintenance-toolbar"><button type="button" className="secondary-button" aria-label={`Проверить ${service.name} сейчас`} disabled={!canCheck} onClick={() => prepare({ ...base, action: 'strategy_check', settings: {} }, service.name)}>Проверить сейчас</button>
        <button type="button" className="secondary-button" aria-label={`Подобрать для ${service.name}`} disabled={!canCheck} onClick={() => prepare({ ...base, action: 'strategy_tune', settings: {} }, service.name)}>Подобрать</button></div>
      <label>Стратегия {service.name}<select value={selection} disabled={!canApply} onChange={event => setSelection(event.target.value as StrategyId)}><option value="" disabled>Нет подтверждённой</option>{snapshot.catalog?.strategies.map(id => <option key={id} value={id}>{id}</option>)}</select></label>
      {selection !== service.strategy_id && <p className="maintenance-hint">Выбрано, но не применено: {selection}</p>}
      {selection !== service.strategy_id && <label>После применения {service.name}<select value={applyMode} onChange={event => setApplyMode(event.target.value as StrategyMode)} disabled={!canApply}><option value="pinned">Закрепить</option><option value="auto">Авто</option></select></label>}
      <div className="maintenance-toolbar"><button type="button" className="primary-button" aria-label={`Применить для ${service.name}`} disabled={!canApply || !selection || selection === service.strategy_id} onClick={() => selection && prepare({ ...base, action: 'strategy_apply', settings: { strategy_id: selection, mode: applyMode } }, service.name)}>Проверить и применить</button>
        <button type="button" className="secondary-button" aria-label={`Закрепить ${service.name}`} disabled={!writable || !service.strategy_id || service.mode === 'pinned'} onClick={() => configure({ ...policy, mode: 'pinned' })}>Закрепить текущую</button>
        <button type="button" className="secondary-button" aria-label={`Включить авто ${service.name}`} disabled={!canApply || !service.strategy_id || (service.enabled && service.mode === 'auto')} onClick={() => configure({ ...policy, enabled: true, mode: 'auto' })}>Вернуть авто</button>
        <button type="button" className="secondary-button" aria-label={`Откатить ${service.name}`} disabled={!canApply || !service.previous_strategy_id} onClick={() => prepare({ ...base, action: 'strategy_rollback', settings: {} }, service.name)}>Вернуть предыдущую</button></div>
      <details><summary>Расписание и история</summary>
        <label className="maintenance-check"><input type="checkbox" aria-label={`Периодические проверки ${service.name}`} checked={policy.enabled} disabled={!writable} onChange={event => setPolicy({ ...policy, enabled: event.target.checked })} />Периодические проверки</label>
        <label>Интервал {service.name}<select value={policy.interval_minutes} disabled={!writable} onChange={event => setPolicy({ ...policy, interval_minutes: Number(event.target.value) as StrategyPolicy['interval_minutes'] })}>{[5, 15, 30, 60].map(minutes => <option key={minutes} value={minutes}>{minutes} мин.</option>)}</select></label>
        <label className="maintenance-check"><input type="checkbox" aria-label={`Суточный подбор ${service.name}`} checked={policy.daily_enabled} disabled={!writable} onChange={event => setPolicy({ ...policy, daily_enabled: event.target.checked })} />Текущая + до двух альтернатив в 05:30</label>
        <button type="button" className="secondary-button" aria-label={`Сохранить расписание ${service.name}`} disabled={!writable || (policy.enabled && policy.mode === 'auto' && !canApply)} onClick={() => configure(policy)}>Сохранить расписание</button>
        <p>Смены за последнее время:</p>{service.history.length ? <ul>{service.history.map((item, index) => <li key={`${item.changed_at}-${index}`}>{maintenanceDate(item.changed_at)} · {item.previous ?? '—'} → {item.strategy} · {item.reason === 'automatic' ? 'автосмена' : item.reason === 'rollback' ? 'откат' : 'вручную'} · {item.actor}</li>)}</ul> : <p>Смен не зафиксировано.</p>}
      </details>
      <details><summary>Результаты попыток ({service.results.length})</summary>{service.results.length ? <ul>{service.results.map((item, index) => <li key={`${item.checked_at}-${index}`}>{maintenanceDate(item.checked_at)} · {item.strategy_id} · {STRATEGY_ERRORS[item.reason] ?? 'Нет подтверждения'}{item.latency_ms != null ? ` · ${Math.round(item.latency_ms)} мс` : ''}</li>)}</ul> : <p>Попыток ещё нет.</p>}</details>
    </div>
  </section>
}
