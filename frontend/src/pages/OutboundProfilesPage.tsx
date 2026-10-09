import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import type { ProfileOperation } from '../api/profileTypes'
import type { CancelOperation, MaintenanceJob } from '../api/maintenanceTypes'
import { AsyncState } from '../components/AsyncState'
import { FINISHED_PHASES, MaintenanceJobPanel, maintenanceDate } from '../components/MaintenanceJobPanel'
import { ProfileProbeResults } from '../components/ProfileProbeResults'
import { StrategyConfirmation } from '../components/StrategyConfirmation'

const role = (target: string) => target === 'WG-IMP' ? 'Основной · VLESS' : 'Резервный · Hysteria2'
type Confirmation = { jobId: string; operation: ProfileOperation | CancelOperation; name: string }

export function OutboundProfilesPage() {
  const client = useQueryClient(), [params, setParams] = useSearchParams()
  const rawId = params.get('job') ?? '', jobId = /^[0-9a-f]{32}$/.test(rawId) ? rawId : ''
  const [uri, setUri] = useState(''), [draftId, setDraftId] = useState(''), [importing, setImporting] = useState(false)
  const [visible, setVisible] = useState(!document.hidden), [confirmation, setConfirmation] = useState<Confirmation | null>(null)
  const [notice, setNotice] = useState<string | null>(null), [error, setError] = useState<string | null>(null)
  const importLock = useRef(false)
  useEffect(() => {
    const change = () => { setVisible(!document.hidden); if (!document.hidden) void client.invalidateQueries({ queryKey: ['outbound-profiles'] }) }
    document.addEventListener('visibilitychange', change)
    return () => document.removeEventListener('visibilitychange', change)
  }, [client])
  const snapshot = useQuery({ queryKey: ['outbound-profiles'], queryFn: api.outboundProfiles, refetchInterval: visible ? 30_000 : false, refetchIntervalInBackground: false, retry: false })
  const jobs = useQuery({ queryKey: ['maintenance', 'jobs'], queryFn: api.maintenanceJobs, refetchInterval: visible ? 30_000 : false, refetchIntervalInBackground: false, retry: false })
  const job = useQuery({ queryKey: ['maintenance', 'job', jobId], queryFn: () => api.maintenanceJob(jobId), enabled: !!jobId && visible,
    refetchInterval: query => !visible || (query.state.data && FINISHED_PHASES.has(query.state.data.phase)) ? false : 5_000, refetchIntervalInBackground: false, retry: false })
  useEffect(() => {
    if (job.data && FINISHED_PHASES.has(job.data.phase)) {
      void client.invalidateQueries({ queryKey: ['outbound-profiles'] }); void client.invalidateQueries({ queryKey: ['maintenance', 'jobs'] })
    }
  }, [job.data?.phase, client])
  const data = snapshot.data
  const observed: MaintenanceJob | null = jobId ? job.data ?? { job_id: jobId, phase: 'unknown', cancel_allowed: false } : null
  const busy = importing || jobs.isLoading || !!jobs.error || !jobs.data?.available || jobs.data.jobs.some(item => !FINISHED_PHASES.has(item.phase)) || (!!observed && !FINISHED_PHASES.has(observed.phase))
  const drafts = [...(data?.drafts ?? [])].sort((a, b) => a.created_at - b.created_at || a.draft_id.localeCompare(b.draft_id))
  const draft = drafts.find(item => item.draft_id === draftId) ?? drafts.at(-1)
  const fresh = !!draft && draft.revision === data?.revision && draft.expires_at > Date.now() / 1000
  const canApply = fresh && draft.check_passed && draft.checked_at !== null && Date.now() / 1000 - draft.checked_at >= 0 && Date.now() / 1000 - draft.checked_at <= 300 && !!draft.endpoint_ip
  async function preview(event: React.FormEvent) {
    event.preventDefault()
    if (importLock.current || busy || !data?.can_manage || !uri.trim()) return
    importLock.current = true; setImporting(true); setError(null); setNotice(null)
    try {
      const result = await api.previewOutboundProfile(uri)
      setUri(''); setDraftId(result.draft_id)
      client.setQueryData(['outbound-profiles'], { ...data, revision: result.revision, drafts: [...drafts, result].slice(-8) })
      setNotice('Ссылка разобрана. Рабочий маршрут пока не изменён.')
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Не удалось разобрать ссылку.') }
    finally { importLock.current = false; setImporting(false) }
  }
  function prepare(action: ProfileOperation['action']) {
    if (!draft || busy || !data?.can_manage || !fresh || (action === 'profile_apply' && !canApply)) return
    setConfirmation({ jobId: crypto.randomUUID().replace(/-/g, ''), operation: { action, draft_id: draft.draft_id, expected_revision: draft.revision }, name: `${role(draft.target)} · ${draft.server}:${draft.port}` })
  }
  async function confirm(password: string) {
    if (!confirmation) return
    const { jobId: id, operation } = confirmation
    const authorization = await api.authorizeMaintenance(operation, password)
    setParams({ job: id }); client.setQueryData(['maintenance', 'job', id], { job_id: id, phase: 'unknown', cancel_allowed: false })
    try {
      const result = operation.action === 'cancel' ? await api.cancelMaintenance(id, authorization.grant) : await api.submitMaintenance(id, operation, authorization.grant)
      if (result.job_id !== id) throw new Error('Unconfirmed job')
      client.setQueryData(['maintenance', 'job', id], result); setNotice('Задание принято. Можно закрыть страницу: проверка продолжится на NAS.')
    } catch {
      setNotice('Ответ потерян или не подтверждён. Проверяем прежний ID; повторный запуск не выполняется.')
      void client.fetchQuery({ queryKey: ['maintenance', 'job', id], queryFn: () => api.maintenanceJob(id), staleTime: 0 }).catch(() => undefined)
    } finally { setConfirmation(null); void client.invalidateQueries({ queryKey: ['maintenance', 'jobs'] }) }
  }
  return <>
    <main className="page components-page profile-page" inert={confirmation ? true : undefined}>
      <header className="page-header"><div><p className="eyebrow">02 / МАРШРУТЫ / КЛЮЧИ</p><h1>Ключи VPN</h1><p>Вставить ссылку → проверить отдельно → применить.</p></div><a href="/routes">К маршрутам</a></header>
      <AsyncState loading={snapshot.isLoading} error={snapshot.error}>
        {!data?.available ? <p className="maintenance-notice">Исполнитель замены ключей ещё не подключён или недоступен. Рабочий VPN не изменён.</p> : <>
          <div className="component-grid">{data.current.map(current => <section className="panel" key={current.target}><div className="section-heading"><h2>{role(current.target)}</h2><span>Сохранённая конфигурация</span></div><div className="maintenance-body"><p className="profile-server">{current.server}:{current.port}</p><p>Доступность маршрута — в разделе «Маршруты».</p></div></section>)}</div>
          {!data.can_manage ? <p className="maintenance-notice">Замена доступна только владельцу панели.</p> : <section className="panel"><div className="section-heading"><h2>Новая ссылка подключения</h2></div><div className="maintenance-body">
            <form className="profile-import" onSubmit={preview}><label htmlFor="profile-uri">Ссылка подключения</label><input id="profile-uri" type="password" autoComplete="off" spellCheck={false} maxLength={8192} value={uri} onChange={event => setUri(event.target.value)} disabled={busy} placeholder="vless://… или hy2://…" required />
              <p className="maintenance-hint">VLESS TCP/REALITY заменяет основной выход. Hysteria2/salamander — резервный. Профили WireGuard и правила сохраняются.</p>
              <button className="primary-button" type="submit" disabled={busy || !uri.trim()}>{importing ? 'Разбор…' : 'Разобрать ссылку'}</button></form>
            {error && <p className="form-error" role="alert">{error}</p>}
          </div></section>}
          {data.can_manage && draft && <section className="panel profile-draft"><div className="section-heading"><h2>Предварительный просмотр</h2><span>Ещё не применён</span></div><div className="maintenance-body">
            {drafts.length > 1 && <label>Черновик<select value={draft.draft_id} onChange={event => setDraftId(event.target.value)} disabled={busy}>{drafts.map(item => <option value={item.draft_id} key={item.draft_id}>{role(item.target)} · {item.server} · {maintenanceDate(item.created_at)}</option>)}</select></label>}
            <dl><dt>Заменится</dt><dd>{role(draft.target)}</dd><dt>Сервер из ссылки</dt><dd className="profile-server">{draft.server}:{draft.port}</dd>
              <dt>Проверенный IP</dt><dd>{draft.endpoint_ip ?? 'Определится при проверке'}</dd><dt>Последняя проверка</dt><dd>{maintenanceDate(draft.checked_at)}</dd><dt>Черновик доступен до</dt><dd>{maintenanceDate(draft.expires_at)}</dd></dl>
            <p>{!fresh ? 'Настройки изменились или черновик истёк. Вставьте ссылку заново.' : canApply ? 'Проверка успешна. Результат действителен пять минут.' : draft.checked_at ? 'Успешная свежая проверка не подтверждена. Применение недоступно.' : 'Сначала проверим новый ключ в отдельном контейнере, не переключая клиентов.'}</p>
            <p className="maintenance-hint">Закрепляется проверенный публичный IP сервера, TLS-имя сохраняется. Если провайдер изменит IP, повторно вставьте и проверьте ссылку.</p>
            <div className="maintenance-toolbar"><button className="secondary-button" type="button" disabled={busy || !fresh} onClick={() => prepare('profile_check')}>Проверить ключ</button><button className="primary-button" type="button" disabled={busy || !canApply} onClick={() => prepare('profile_apply')}>Применить ключ</button></div>
            {!observed?.profile?.results.length && <ProfileProbeResults results={draft.results} />}
          </div></section>}
        </>}
      </AsyncState>
      {notice && <p className="maintenance-notice" role="status">{notice}</p>}
      {observed && <MaintenanceJobPanel job={observed} onCancel={data?.can_manage && observed.cancel_allowed ? () => setConfirmation({ jobId: observed.job_id, operation: { action: 'cancel', job_id: observed.job_id }, name: 'Замена ключа VPN' }) : undefined} />}
    </main>
    {confirmation && <StrategyConfirmation operation={confirmation.operation} serviceName={confirmation.name} onConfirm={confirm} onClose={() => setConfirmation(null)} />}
  </>
}
