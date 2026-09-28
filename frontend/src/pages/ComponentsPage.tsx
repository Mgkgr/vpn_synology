import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'

import { api } from '../api/client'
import type { CancelOperation, ComponentId, ComponentInventory, MaintenanceJob, MaintenanceOperation } from '../api/maintenanceTypes'
import { AsyncState } from '../components/AsyncState'
import { COMPONENT_NAMES, FINISHED_PHASES, MaintenanceJobPanel, maintenanceDate } from '../components/MaintenanceJobPanel'
import { MaintenanceConfirmation } from '../components/MaintenanceConfirmation'
import { formatDate } from '../components/Status'

type Confirmation = { operation: MaintenanceOperation | CancelOperation; jobId: string; affected: ComponentId[]; stopped: ComponentId[] }
const JOB_ID = /^[0-9a-f]{32}$/
function approvedRelease(item: ComponentInventory) {
  return item.release?.releases.find(release => release.compatibility === 'approved' && /^sha256:[0-9a-f]{64}$/.test(release.digest ?? ''))
}
function releaseNotes(url: string | null | undefined): string | undefined {
  if (!url) return undefined
  try { const parsed = new URL(url); return parsed.protocol === 'https:' && parsed.hostname === 'github.com' && !parsed.username && !parsed.password ? parsed.href : undefined }
  catch { return undefined }
}

export function ComponentsPage() {
  const queryClient = useQueryClient()
  const [params, setParams] = useSearchParams()
  const rawId = params.get('job') ?? ''
  const jobId = JOB_ID.test(rawId) ? rawId : ''
  const [visible, setVisible] = useState(!document.hidden)
  const [selection, setSelection] = useState<ComponentId[]>([])
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [checking, setChecking] = useState(false)
  const [checkAfter, setCheckAfter] = useState(0)
  const [snapshotId, setSnapshotId] = useState('')
  const watchStarted = useRef(Date.now())
  useEffect(() => {
    if (!checkAfter) return
    const timer = window.setTimeout(() => setCheckAfter(0), Math.max(0, checkAfter - Date.now()))
    return () => window.clearTimeout(timer)
  }, [checkAfter])
  useEffect(() => { watchStarted.current = Date.now() }, [jobId])
  useEffect(() => {
    const change = () => { setVisible(!document.hidden); if (!document.hidden) void queryClient.invalidateQueries({ queryKey: ['maintenance'] }) }
    document.addEventListener('visibilitychange', change)
    return () => document.removeEventListener('visibilitychange', change)
  }, [queryClient])
  const inventory = useQuery({ queryKey: ['maintenance', 'components'], queryFn: api.maintenanceComponents, refetchInterval: visible ? 60_000 : false, refetchIntervalInBackground: false, retry: false })
  const history = useQuery({ queryKey: ['maintenance', 'jobs'], queryFn: api.maintenanceJobs, refetchInterval: visible ? 60_000 : false, retry: false })
  const job = useQuery({ queryKey: ['maintenance', 'job', jobId], queryFn: () => api.maintenanceJob(jobId), enabled: !!jobId && visible,
    refetchInterval: query => !visible || (query.state.data && FINISHED_PHASES.has(query.state.data.phase)) ? false : Date.now() - watchStarted.current < 30_000 ? 2_000 : 5_000,
    refetchIntervalInBackground: false, retry: false })
  const rows = inventory.data?.components ?? []
  const ready = !!inventory.data?.can_maintain && /^[0-9a-f]{64}$/.test(inventory.data?.revision ?? '')
  const active = history.data?.jobs.some(item => !FINISHED_PHASES.has(item.phase)) || (!!jobId && (!job.data || !FINISHED_PHASES.has(job.data.phase)))
  const selected = rows.filter(row => selection.includes(row.component))
  const snapshot = inventory.data?.backup_snapshots?.find(item => item.snapshot_id === snapshotId)
  const observedJob: MaintenanceJob | null = jobId ? job.data ?? { job_id: jobId, phase: 'unknown', cancel_allowed: false } : null

  useEffect(() => {
    if (job.data && FINISHED_PHASES.has(job.data.phase)) {
      void queryClient.invalidateQueries({ queryKey: ['maintenance', 'components'] })
      void queryClient.invalidateQueries({ queryKey: ['maintenance', 'jobs'] })
    }
  }, [job.data?.phase, queryClient])

  function prepare(action: MaintenanceOperation['action'], components: ComponentId[]) {
    if (!ready || active || !components.length) return
    const affected = [...components]
    if (components.includes('wireguard') && rows.some(row => row.component === 'mihomo' && row.running) && !affected.includes('mihomo')) affected.push('mihomo')
    const releases: MaintenanceOperation['release_ids'] = {}
    if (action === 'update') {
      for (const id of components) {
        const row = rows.find(item => item.component === id)
        const release = row && approvedRelease(row)
        if (!release) return
        releases[id] = release.release_id
      }
    }
    if (action === 'rollback' && (!snapshot || components.some(id => !snapshot.components.includes(id)))) return
    setConfirmation({ jobId: crypto.randomUUID().replace(/-/g, ''), affected,
      stopped: rows.filter(row => components.includes(row.component) && !row.running).map(row => row.component),
      operation: { action, components, expected_revision: inventory.data!.revision!, release_ids: releases, enable_stopped: [],
        snapshot_id: action === 'rollback' ? snapshotId : null, accept_data_loss: false } })
  }

  async function confirm(operation: MaintenanceOperation | CancelOperation, password: string) {
    if (!confirmation) return
    // Auth may be retried manually, but once granted this POST is sent at most once.
    const authorization = await api.authorizeMaintenance(operation, password)
    const id = confirmation.jobId
    setParams({ job: id })
    queryClient.setQueryData(['maintenance', 'job', id], { job_id: id, phase: 'unknown', cancel_allowed: false })
    try {
      const result = operation.action === 'cancel' ? await api.cancelMaintenance(id, authorization.grant) : await api.submitMaintenance(id, operation, authorization.grant)
      if (result.job_id !== id) throw new Error('Unconfirmed job id')
      queryClient.setQueryData(['maintenance', 'job', id], result)
      setNotice('Задание принято. Его ID сохранён в адресе страницы.')
    } catch {
      setNotice('Ответ потерян или не подтверждён. Проверяем прежний ID; повторный запуск не выполняется.')
    } finally {
      setConfirmation(null)
      void queryClient.invalidateQueries({ queryKey: ['maintenance', 'jobs'] })
    }
  }
  async function checkReleases() {
    if (checking || Date.now() < checkAfter) return
    setChecking(true); setCheckAfter(Date.now() + 60_000)
    try { await api.checkComponentReleases(); setNotice('Проверка версий запрошена. Установка автоматически не начнётся.'); void queryClient.invalidateQueries({ queryKey: ['maintenance', 'components'] }) }
    catch (error) { setNotice(error instanceof Error ? error.message : 'Проверка недоступна.') }
    finally { setChecking(false) }
  }

  return <>
    <main className="page components-page" inert={confirmation ? true : undefined}>
      <header className="page-header"><div><p className="eyebrow">08 / СИСТЕМА</p><h1>Компоненты</h1><p>Версии и обслуживание VPN-комплекса. Перезагрузка NAS сюда не входит.</p></div></header>
      {notice && <p role="status" className="maintenance-notice">{notice}</p>}
      {inventory.data && !inventory.data.available && <p className="maintenance-notice">Исполнитель обслуживания ещё не подключён или недоступен. Работающий VPN не изменён.</p>}
      {inventory.data?.available && !ready && <p className="maintenance-notice">Режим просмотра: изменение компонентов доступно владельцу после проверки и настройки исполнителя.</p>}
      <div className="maintenance-toolbar">
        <button type="button" className="secondary-button" disabled={!inventory.data?.can_check || checking || Date.now() < checkAfter} onClick={() => void checkReleases()}>{checking ? 'Запрос проверки…' : 'Проверить версии'}</button>
        <button type="button" className="secondary-button" disabled={!ready || active || !rows.some(row => row.installed && row.running)} onClick={() => prepare('restart', rows.filter(row => row.installed && row.running).map(row => row.component))}>Перезапустить комплекс</button>
        <button type="button" className="primary-button" disabled={!ready || active || !selected.length || selected.some(row => !approvedRelease(row))} onClick={() => prepare('update', selected.map(row => row.component))}>Обновить выбранные ({selected.length})</button>
      </div>
      <p className="maintenance-hint">Проверка версий не устанавливает обновления. Остановленные и отсутствующие компоненты не запускаются при перезапуске комплекса.</p>
      <AsyncState loading={inventory.isLoading} error={inventory.error} empty={!rows.length} emptyLabel="Пока нет подтверждённого списка компонентов.">
        <div className="component-grid">{rows.map(row => {
          const name = COMPONENT_NAMES[row.component], release = row.release?.releases[0], approved = approvedRelease(row)
          const drift = row.actual_digest && row.expected_digest ? row.actual_digest !== row.expected_digest : null
          const notes = releaseNotes(release?.release_notes_url)
          return <section className="panel component-card" key={row.component}>
            <div className="section-heading"><h2>{name}</h2><span>{!row.installed ? 'Не установлен' : row.running ? 'Запущен' : 'Остановлен'}</span></div>
            <div className="maintenance-body"><p>Установлено: <strong>{row.actual_version ?? 'Версия не подтверждена'}</strong></p>
              <p>{row.release?.checked_at ? `Версии проверены: ${formatDate(row.release.checked_at)}` : 'Обновления ещё не проверялись'}</p>
              {(row.freshness_error || row.release?.freshness_error) && <p className="form-error">Сведения могут быть устаревшими; последняя проверка не завершена.</p>}
              <p>{release ? `Доступный релиз: ${release.version}${approved ? ' · есть разрешённая версия' : ' · совместимость и откат не подтверждены'}` : 'Подтверждённых сведений о новом релизе нет'}</p>
              {notes && <a href={notes} target="_blank" rel="noreferrer">Изменения релиза</a>}
              <details><summary>Образы и контроль изменений</summary><p>{drift === null ? 'Сравнение образов пока недоступно' : drift ? 'Обнаружено расхождение с закреплённым образом' : 'Образ совпадает с закреплённым'}</p>
                <dl><dt>Фактический digest</dt><dd><code>{row.actual_digest ?? 'Не подтверждён'}</code></dd><dt>Закреплённый digest</dt><dd><code>{row.expected_digest ?? 'Не задан'}</code></dd></dl>
                {row.artifacts.map(artifact => <p key={artifact.service}>{artifact.service}: <code>{artifact.actual_digest ?? 'Не подтверждён'}</code></p>)}
              </details>
              <label className="maintenance-check"><input type="checkbox" aria-label={`Выбрать ${name}`} checked={selection.includes(row.component)} disabled={!ready || active || !row.installed} onChange={event => setSelection(items => event.target.checked ? [...items, row.component] : items.filter(id => id !== row.component))} />В пакетное обновление</label>
              <div className="maintenance-toolbar"><button type="button" className="secondary-button" aria-label={`Перезапустить ${name}`} disabled={!ready || active || !row.installed || drift === true} onClick={() => prepare('restart', [row.component])}>Перезапустить</button>
                <button type="button" className="secondary-button" aria-label={`Обновить ${name}`} disabled={!ready || active || !row.installed || !approved || drift === true} onClick={() => prepare('update', [row.component])}>Обновить</button></div>
            </div>
          </section>
        })}</div>
      </AsyncState>
      <section className="panel"><div className="section-heading"><h2>Проверенные резервные копии</h2></div><div className="maintenance-body">
        <label>Копия для восстановления<select value={snapshotId} onChange={event => setSnapshotId(event.target.value)} disabled={!ready || active}><option value="">Выберите точную копию</option>{inventory.data?.backup_snapshots?.map(item => <option value={item.snapshot_id} key={item.snapshot_id}>{maintenanceDate(item.verified_at)} · {item.snapshot_id.slice(0, 12)}</option>)}</select></label>
        <p>Возврат выбранных компонентов требует повторного пароля и подтверждения возможной потери более новых данных. Ключи не перевыпускаются.</p>
        <button type="button" className="secondary-button" disabled={!ready || active || !snapshot || !selected.length || selected.some(row => !snapshot.components.includes(row.component))} onClick={() => prepare('rollback', selected.map(row => row.component))}>Восстановить выбранные из копии</button>
      </div></section>
      {observedJob && <MaintenanceJobPanel job={observedJob} onCancel={ready && observedJob.cancel_allowed ? () => setConfirmation({ operation: { action: 'cancel', job_id: jobId }, jobId, affected: observedJob.component ? [observedJob.component] : [], stopped: [] }) : undefined} />}
      {job.error && <p role="alert" className="maintenance-notice">Связь с исполнителем потеряна. Это не означает, что задание остановлено или завершено.</p>}
      <section className="panel"><div className="section-heading"><h2>Последние задания</h2></div><div className="maintenance-body"><AsyncState loading={history.isLoading} error={history.error} empty={!history.data?.jobs.length} emptyLabel="Заданий пока нет.">
        <ul className="maintenance-history">{history.data?.jobs.map(item => <li key={item.job_id}><button type="button" className="text-button" onClick={() => setParams({ job: item.job_id })}>{item.job_id.slice(0, 12)}</button> · {maintenanceDate(item.created_at)} · {item.actor ?? 'Владелец'} · {item.phase}</li>)}</ul>
      </AsyncState></div></section>
    </main>
    {confirmation && <MaintenanceConfirmation key={confirmation.jobId + confirmation.operation.action} operation={confirmation.operation} affected={confirmation.affected} stopped={confirmation.stopped} onConfirm={confirm} onClose={() => setConfirmation(null)} />}
  </>
}
