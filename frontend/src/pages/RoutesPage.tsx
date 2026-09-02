import { FormEvent, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import type { ProbeDiagnosis, ProbeTarget, RouteProbe, RoutesResponse, RouteSwitch } from '../api/types'
import { AsyncState } from '../components/AsyncState'
import { formatDate, Status } from '../components/Status'

export interface RouteViewData {
  primary: 'WG-IMP'
  reserve: 'HY2-USA'
  selected: 'WG-IMP' | 'HY2-USA' | null
  groupName: string
  probes: RouteProbe[]
  lastSwitch: RouteSwitch | null
}

export function routeFixture({ active = 'WG-IMP' }: { active?: 'WG-IMP' | 'HY2-USA' } = {}): RouteViewData {
  return { primary: 'WG-IMP', reserve: 'HY2-USA', selected: active, groupName: 'AUTO', probes: [], lastSwitch: null }
}

export function toRouteViewData(data: RoutesResponse): RouteViewData {
  const group = data.groups.find((item) => item.choices.includes('WG-IMP') && item.choices.includes('HY2-USA'))
  return { primary: 'WG-IMP', reserve: 'HY2-USA', selected: data.fallback.selected, groupName: group?.name ?? 'AUTO', probes: data.probes, lastSwitch: data.last_switch }
}

export function RoutesPage({ data }: { data?: RouteViewData }) {
  const query = useQuery({ queryKey: ['routes'], queryFn: api.routes, enabled: data === undefined })
  const probeTargets = useQuery({ queryKey: ['probe-targets'], queryFn: api.probeTargets, enabled: data === undefined })
  const queryClient = useQueryClient()
  const [runMessage, setRunMessage] = useState<string | null>(null)
  const updateTargets = useMutation({
    mutationFn: (targets: Pick<ProbeTarget, 'key' | 'enabled'>[]) => api.updateProbeTargets(targets),
    onSuccess: (targets) => queryClient.setQueryData(['probe-targets'], targets),
  })
  const runProbes = useMutation({
    mutationFn: api.runProbes,
    onSuccess: () => setRunMessage('Проверка запущена. Результаты появятся в журнале после завершения.'),
  })
  const view = data ?? (query.data ? toRouteViewData(query.data) : undefined)
  const [inspected, setInspected] = useState<'WG-IMP' | 'HY2-USA' | null>(null)
  const inspectedExit = inspected ?? view?.selected ?? 'WG-IMP'

  return (
    <main className="page routes-page">
      <header className="page-header">
        <div><p className="eyebrow">02 / ТОПОЛОГИЯ</p><h1>Маршруты</h1><p>Основной и резервный выходы fallback-группы показаны отдельными ветками.</p></div>
        <div className="header-status"><Status ok={view?.selected ? true : null} pending={!view?.selected} /><span>Группа {view?.groupName ?? '…'}</span></div>
      </header>
      <AsyncState loading={query.isLoading} error={query.error} empty={!view} emptyLabel="Маршруты пока не получены.">
        {view && <>
          <RouteMap data={view} inspected={inspectedExit} onSelect={setInspected} onRun={() => { setRunMessage(null); runProbes.mutate() }} running={runProbes.isPending} runMessage={runMessage} runError={runProbes.error} diagnosisTargets={probeTargets.data ?? []} />
          {probeTargets.data && <ProbeTargetSettings targets={probeTargets.data} pending={updateTargets.isPending} error={updateTargets.error} onChange={(key, enabled) => updateTargets.mutate(probeTargets.data.map((item) => ({ key: item.key, enabled: item.key === key ? enabled : item.enabled })))} onTargetsChange={(targets) => queryClient.setQueryData(['probe-targets'], targets)} />}
        </>}
      </AsyncState>
    </main>
  )
}

function ProbeTargetSettings({ targets, pending, error, onChange, onTargetsChange }: { targets: ProbeTarget[]; pending: boolean; error: Error | null; onChange: (key: string, enabled: boolean) => void; onTargetsChange: (targets: ProbeTarget[]) => void }) {
  const [label, setLabel] = useState('')
  const [url, setUrl] = useState('')
  const [editing, setEditing] = useState<ProbeTarget | null>(null)
  const create = useMutation({
    mutationFn: api.createProbeTarget,
    onSuccess: (target) => { onTargetsChange([...targets, target]); setLabel(''); setUrl('') },
  })
  const update = useMutation({
    mutationFn: ({ key, value }: { key: string; value: { label: string; url: string; enabled: boolean } }) => api.updateProbeTarget(key, value),
    onSuccess: (target) => { onTargetsChange(targets.map((item) => item.key === target.key ? target : item)); setEditing(null); setLabel(''); setUrl('') },
  })
  const remove = useMutation({
    mutationFn: api.deleteProbeTarget,
    onSuccess: (_unused, key) => onTargetsChange(targets.filter((item) => item.key !== key)),
  })
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (editing) update.mutate({ key: editing.key, value: { label, url, enabled: editing.enabled } })
    else create.mutate({ label, url })
  }
  const mutationError = create.error ?? update.error ?? remove.error ?? error
  const busy = pending || create.isPending || update.isPending || remove.isPending
  return <section className="panel probe-targets" aria-labelledby="probe-targets-title">
    <div className="section-heading"><div><h2 id="probe-targets-title">Сайты для проверок</h2><span>Через каждый выход раз в 5 минут; собственные — только публичный HTTPS на порту 443.</span></div></div>
    <div className="probe-target-grid">
      {targets.map((target) => <div key={target.key} className="probe-target"><label><input type="checkbox" checked={target.enabled} disabled={busy} onChange={(event) => onChange(target.key, event.target.checked)} /><span><strong>{target.label}</strong><small>{target.url}</small></span></label>{target.is_custom && <span className="inline-actions"><button type="button" onClick={() => { setEditing(target); setLabel(target.label); setUrl(target.url) }}>Изменить</button><button type="button" onClick={() => remove.mutate(target.key)} disabled={busy}>Удалить</button></span>}</div>)}
    </div>
    <form className="probe-target-form" onSubmit={submit}>
      <label>Название сайта<input aria-label="Название сайта" value={label} onChange={(event) => setLabel(event.target.value)} maxLength={255} required /></label>
      <label>HTTPS-адрес<input aria-label="HTTPS-адрес" type="url" value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://status.example.com/health" maxLength={255} required /></label>
      <div className="inline-actions"><button type="submit" disabled={busy}>{editing ? 'Сохранить' : 'Добавить сайт'}</button>{editing && <button type="button" onClick={() => { setEditing(null); setLabel(''); setUrl('') }}>Отмена</button>}</div>
    </form>
    {mutationError && <p className="form-error" role="alert">{mutationError.message}</p>}
  </section>
}

function RouteMap({ data, inspected, onSelect, onRun, running, runMessage, runError, diagnosisTargets }: { data: RouteViewData; inspected: 'WG-IMP' | 'HY2-USA'; onSelect: (exit: 'WG-IMP' | 'HY2-USA') => void; onRun: () => void; running: boolean; runMessage: string | null; runError: Error | null; diagnosisTargets: ProbeTarget[] }) {
  const isReserveActive = data.selected === data.reserve
  const inspectedRole = inspected === data.primary ? 'Основной' : 'Резервный'
  const probes = data.probes.filter((probe) => probe.target === inspected)
  const recent = probes.slice(0, 2)
  const older = probes.slice(2)
  const [diagnosticTargetKey, setDiagnosticTargetKey] = useState('')
  const diagnosis = useMutation({ mutationFn: api.diagnoseProbe })
  const availableTargets = diagnosisTargets.filter((item) => item.enabled)
  const diagnosticTarget = availableTargets.find((item) => item.key === diagnosticTargetKey) ?? availableTargets[0]
  return <div className="route-layout">
    <section className="route-map-panel" aria-labelledby="route-map-title">
      <div className="section-heading"><h2 id="route-map-title">Путь трафика</h2><span>сейчас: {data.selected ?? 'нет данных'}</span></div>
      <div className="route-role-summary" aria-label="Роли выходов"><span>Основной: {data.primary}</span><span>Резервный: {data.reserve}</span>{isReserveActive && <span>переключён на резерв</span>}</div>
      <div className="route-map" role="group" aria-label="Ветки маршрутизации"><svg className="route-lines" viewBox="0 0 760 260" preserveAspectRatio="none" aria-hidden="true"><path d="M 160 130 H 360" /><path d="M 360 130 C 430 130 430 65 540 65" /><path className="reserve-line" d="M 360 130 C 430 130 430 195 540 195" /></svg><div className="route-node client-node"><strong>WireGuard</strong><span>клиенты</span></div><div className="route-node mihomo-node"><strong>Mihomo</strong><span>fallback-группа</span></div><button type="button" className={`route-node exit-node primary ${inspected === data.primary ? 'selected' : ''}`} onClick={() => onSelect(data.primary)}><span className="branch-key">ОСНОВНОЙ</span><strong>{data.primary}</strong><span>{data.selected === data.primary ? 'активный выход' : 'готов к работе'}</span></button><button type="button" className={`route-node exit-node reserve ${inspected === data.reserve ? 'selected' : ''}`} onClick={() => onSelect(data.reserve)}><span className="branch-key">РЕЗЕРВ</span><strong>{data.reserve}</strong><span>{isReserveActive ? 'активный резерв' : 'только при отказе'}</span></button></div>
      <div className="route-legend" aria-label="Легенда"><span><i className="solid-line" />Основная ветка</span><span><i className="dotted-line" />Резервная ветка</span></div>
    </section>
    <aside className="inspector" aria-labelledby="inspector-title"><p className="eyebrow">ИНСПЕКТОР ВЫХОДА</p><h2 id="inspector-title">{inspected}</h2><dl className="metric-list"><div><dt>Роль</dt><dd>{inspectedRole}</dd></div><div><dt>Последняя смена</dt><dd>{data.lastSwitch ? formatDate(data.lastSwitch.observed_at) : 'нет данных'}</dd></div><div><dt>Текущее состояние</dt><dd>{data.selected === inspected ? 'выбран fallback-группой' : 'не выбран fallback-группой'}</dd></div></dl><div className="probe-run"><button type="button" onClick={onRun} disabled={running}>{running ? 'Запуск…' : 'Проверить сейчас'}</button>{runMessage && <p role="status">{runMessage}</p>}{runError && <p className="form-error" role="alert">{runError.message}</p>}</div><div className="probe-diagnosis"><h3>Диагностика причины</h3><p>Это не ICMP-ping: проверяются API Mihomo, его DNS и конкретный VPN-выход.</p>{diagnosticTarget ? <><label>Сайт <select aria-label="Сайт для диагностики" value={diagnosticTarget.key} onChange={(event) => setDiagnosticTargetKey(event.target.value)} disabled={diagnosis.isPending}>{availableTargets.map((target) => <option value={target.key} key={target.key}>{target.label} — {target.url}</option>)}</select></label><button type="button" onClick={() => diagnosis.mutate({ targetKey: diagnosticTarget.key, outbound: inspected })} disabled={diagnosis.isPending}>{diagnosis.isPending ? 'Диагностика…' : `Диагностировать ${diagnosticTarget.label} через ${inspected}`}</button></> : <p className="state-message">Включите хотя бы один сайт в списке ниже.</p>}{diagnosis.error && <p className="form-error" role="alert">{diagnosis.error.message}</p>}{diagnosis.data && <ProbeDiagnosisResult diagnosis={diagnosis.data} />}</div><h3>Последние проверки</h3><ProbeRows probes={recent} />{older.length > 0 && <details><summary>Показать ещё {older.length}</summary><ProbeRows probes={older} /></details>}</aside>
  </div>
}

function ProbeDiagnosisResult({ diagnosis }: { diagnosis: ProbeDiagnosis }) {
  const addresses = diagnosis.dns.addresses.length ? diagnosis.dns.addresses.join(', ') : 'нет публичного IPv4-адреса'
  return <div className="probe-diagnosis-result" role="status"><p><strong>{diagnosis.conclusion_text}</strong></p><small>{formatDate(diagnosis.observed_at)}</small><ul><li>Контроллер Mihomo: {diagnosticStepText(diagnosis.controller)}</li><li>DNS Mihomo: {diagnosis.dns.hostname || 'не выполнен'}{diagnosis.dns.hostname ? ` → ${addresses}` : ''}{diagnosis.dns.reason ? ` · ${diagnosis.dns.reason}` : ''}</li><li>Выход {diagnosis.outbound}: {diagnosticStepText(diagnosis.exit)}</li></ul></div>
}

function diagnosticStepText(step: ProbeDiagnosis['controller']): string {
  if (step.succeeded === true) return step.latency_ms === null ? 'в норме' : `${step.latency_ms} мс`
  if (step.succeeded === null) return step.reason ?? 'не запускалась'
  return step.reason ?? 'ошибка'
}

function ProbeRows({ probes }: { probes: RouteProbe[] }) {
  if (!probes.length) return <p className="state-message">Нет данных.</p>
  return <table className="compact-table"><tbody>{probes.map((probe, index) => <tr key={`${probe.observed_at}-${index}`}><th scope="row"><span>{probe.endpoint ?? 'endpoint не указан'}</span><small>{formatDate(probe.observed_at)}</small>{!probe.succeeded && probe.reason && <small>{probe.reason}</small>}</th><td>{probe.succeeded ? `${probe.latency_ms ?? '—'} мс` : 'ошибка'}</td></tr>)}</tbody></table>
}
