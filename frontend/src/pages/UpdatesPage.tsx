import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import type { GeoAsset } from '../api/types'
import { AsyncState } from '../components/AsyncState'
import { formatDate, Status } from '../components/Status'

export function UpdatesPage() {
  const updates = useQuery({ queryKey: ['updates'], queryFn: api.updates })
  const assetGroups = useMemo(() => groupAssets(updates.data?.assets ?? []), [updates.data?.assets])
  return <main className="page"><header className="page-header"><div><p className="eyebrow">05 / ГЕОДАННЫЕ</p><h1>Обновления</h1><p>Показывается фактическое время файлов GeoData и изменения правил, без секретов конфигурации. Обновление GeoSite/GeoIP — в разделе «Правила».</p></div></header>
    <section className="panel"><div className="section-heading"><h2>GeoData на шлюзе</h2><span>время последнего наблюдения каждого файла</span></div><AsyncState loading={updates.isLoading} error={updates.error} empty={!assetGroups.length} emptyLabel="Файлы GeoData ещё не были обнаружены.">{updates.data?.assets_error && <p className="form-error" role="alert">{updates.data.assets_error}</p>}<div className="geodata-groups">{assetGroups.map((group) => <div key={group.observedAt} className="geodata-group"><strong>Обновлено {formatDate(group.observedAt)}</strong><div>{group.assets.map((asset) => <span key={asset.filename}>{asset.kind} · {asset.filename}</span>)}</div></div>)}</div></AsyncState></section>
    <section className="panel"><div className="section-heading"><h2>Изменения правил</h2><span>кто и что изменил</span></div><AsyncState loading={updates.isLoading} error={updates.error} empty={!updates.data?.rule_changes?.length} emptyLabel="Изменений правил пока нет."><table className="data-table"><thead><tr><th>Время</th><th>Администратор</th><th>Действие</th><th>Правило</th><th>Результат</th></tr></thead><tbody>{updates.data?.rule_changes?.map((change, index) => <tr key={`${change.observed_at}-${index}`}><td>{formatDate(change.observed_at)}</td><td>{change.actor}</td><th scope="row">{change.action}</th><td>{change.subject}</td><td><Status ok={change.succeeded} pending={change.succeeded === null} /></td></tr>)}</tbody></table></AsyncState></section>
    <section className="panel"><div className="section-heading"><h2>Аудит обновлений</h2><span>версия, результат и код контроллера</span></div><AsyncState loading={updates.isLoading} error={updates.error} empty={!updates.data?.updates.length} emptyLabel="Операций обновления пока нет.">{updates.data && <table className="data-table"><thead><tr><th>Время</th><th>Операция</th><th>Версия</th><th>HTTP</th><th>Результат</th></tr></thead><tbody>{updates.data.updates.map((update) => <tr key={update.id}><td>{formatDate(update.observed_at)}</td><th scope="row">{update.operation ?? 'geo upgrade'}</th><td>{update.version ?? 'не указана'}</td><td>{update.status_code ?? '—'}</td><td><Status ok={update.succeeded} /></td></tr>)}</tbody></table>}</AsyncState></section>
  </main>
}

function groupAssets(assets: GeoAsset[]): { observedAt: string; assets: GeoAsset[] }[] {
  const groups = new Map<string, GeoAsset[]>()
  for (const asset of assets) {
    const observedAt = asset.last_observed_at ?? asset.modified_at
    groups.set(observedAt, [...(groups.get(observedAt) ?? []), asset])
  }
  return [...groups.entries()].sort(([left], [right]) => right.localeCompare(left)).map(([observedAt, grouped]) => ({ observedAt, assets: grouped }))
}
