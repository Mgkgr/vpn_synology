import { useIsMutating, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { outboundLabel } from '../api/outboundLabels'
import { CategoryRules } from '../features/rules/CategoryRules'
import { DirectRulesEditor } from '../features/rules/DirectRulesEditor'
import { GeoDataStatus, geoUpdateSummary } from '../features/rules/GeoDataStatus'
import './RulesPage.css'

export function RulesPage() {
  const client = useQueryClient()
  const rules = useQuery({ queryKey: ['rules'], queryFn: api.rules, staleTime: 15_000 })
  const updates = useQuery({ queryKey: ['updates'], queryFn: api.updates, staleTime: 30_000 })
  const probes = useQuery({ queryKey: ['service-checks'], queryFn: api.siteProbes, staleTime: 60_000, refetchInterval: 60_000, refetchIntervalInBackground: false, retry: false })
  const upgrade = useMutation({
    mutationKey: ['rules-write'],
    mutationFn: api.updateGeo,
    onSettled: () => {
      void client.invalidateQueries({ queryKey: ['rules'] })
      void client.invalidateQueries({ queryKey: ['updates'] })
    },
  })
  const geoBusy = upgrade.isPending || updates.data?.updates.find((update) => update.operation === 'geo_upgrade')?.verification === 'pending'
  const writeBusy = useIsMutating({ mutationKey: ['rules-write'] }) > 0 || geoBusy
  return <main className="page rules-page">
    <header className="page-header"><div><p className="eyebrow">04 / ПРАВИЛА MIHOMO</p><h1>Правила</h1><p>Назначения в плитках, собственные сайты и состояние GeoData.</p>{rules.isLoading && <p role="status">Загружаем GeoSite, GeoIP и правила Mihomo…</p>}</div><div className="rules-header-actions">
      <button type="button" className="primary-button" disabled={writeBusy} onClick={() => upgrade.mutate()}>{geoBusy ? 'Обновление выполняется…' : 'Обновить GeoSite / GeoIP'}</button>
      <button type="button" className="text-button" disabled={rules.isFetching || updates.isFetching || probes.isFetching} onClick={() => { void rules.refetch(); void updates.refetch(); void probes.refetch() }}>Обновить сведения</button>
      {upgrade.isPending && <p role="status">Запрос отправлен. Ожидаем результат; процент выполнения Mihomo не сообщает.</p>}
      {upgrade.error && <p role="alert" className="form-error">Ответ не получен. Результат запроса неизвестен; обновите сведения перед повтором.</p>}
      {upgrade.isSuccess && <p role="status">{geoUpdateSummary(upgrade.data)}</p>}
    </div></header>
    {rules.error && <p role="alert" className="form-error">Не удалось прочитать правила. {rules.data ? 'Показан предыдущий снимок; изменения заблокированы до успешного чтения.' : 'Обновите сведения.'}</p>}
    {Object.entries(rules.data?.section_errors ?? {}).filter(([key]) => key !== 'direct').map(([key, message]) => <p role="alert" className="form-error" key={key}>{message}</p>)}
    <GeoDataStatus data={updates.data} loading={updates.isLoading} error={updates.error} />
    <CategoryRules data={rules.data} disabled={Boolean(rules.error) || writeBusy} probes={probes.data} probesLoading={probes.isLoading} probesError={Boolean(probes.error)} />
    <DirectRulesEditor serverText={rules.data?.direct_text} serverHash={rules.data?.direct_sha256} error={rules.data?.section_errors?.direct} disabled={Boolean(rules.error) || writeBusy} />
    <details className="panel rules-controller-details"><summary>Технические сведения Mihomo · источники и активные правила</summary>
      <div className="rules-workspace-body"><h2>Rule providers</h2><p className="rules-hint">Наличие провайдера в контроллере не подтверждает актуальность его источника.</p><div className="policy-table-wrap"><table className="data-table"><thead><tr><th>Имя</th><th>Поведение</th></tr></thead><tbody>{rules.data?.providers.map((provider) => <tr key={provider.name}><th scope="row">{provider.name}</th><td>{provider.behavior}</td></tr>)}</tbody></table></div>
        <h2>Активные правила контроллера</h2><p className="rules-hint">Порядок важен: первое совпадение определяет выход. Пустой снимок при ошибке не означает отсутствие правил.</p><div className="policy-table-wrap"><table className="data-table"><thead><tr><th>Тип</th><th>Значение</th><th>Выход</th></tr></thead><tbody>{rules.data?.rules.map((rule, index) => <tr key={index}><td>{rule.type}</td><td>{rule.payload}</td><td>{outboundLabel(rule.proxy)}</td></tr>)}</tbody></table></div>
      </div>
    </details>
  </main>
}
