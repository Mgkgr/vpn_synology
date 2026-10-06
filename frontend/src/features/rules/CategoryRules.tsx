import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'

import { api } from '../../api/client'
import type { ManagedRuleCategory, ManagedRuleInput, ManagedRulePolicy, RulesResponse, ServiceProbeResponse } from '../../api/types'
import { actionLabel, reconcileRules, routeChoices } from './ruleState'
import { ServiceProbeStatus, ServiceProbeSummary } from './ServiceProbeStatus'

const groups = [
  { title: 'Российские сервисы', description: 'Широкое правило направит весь набор российских доменов. Для банков и магазинов лучше выбрать точный сервис.', categories: ['category-bank-ru', 'category-ecommerce-ru', 'category-retail-ru', 'category-gov-ru', 'category-entertainment-ru', 'category-media-ru', 'category-travel-ru', 'category-medicine-ru', 'category-ru', 'ozon', 'wildberries', 'avito', 'sber', 'tbank-ru', 'yandex', 'kinopoisk', 'vk', 'mailru-group', 'cdek', 'megafon', 'mts-ru', 'rostelecom', 't2-ru'] },
  { title: 'P2P / торренты', description: 'Домены трекеров, не распознавание всего BitTorrent-трафика.', categories: ['category-public-tracker', 'category-pt', 'tracker'] },
  { title: 'Видео и стриминг', description: 'Широкие категории могут затронуть общую инфраструктуру и CDN.', categories: ['category-entertainment', 'category-media', 'disney', 'hbo', 'primevideo', 'twitch', 'dazn', 'bilibili', 'biliintl'] },
  { title: 'Игры и загрузки', description: 'Платформы, обновления и источники загрузок.', categories: ['category-games', 'category-game-platforms-download', 'category-android-app-download', 'steam'] },
  { title: 'Инфраструктура и специальные категории', description: 'Правила задают маршрут, но не блокируют домены.', categories: ['anime', 'ehentai', 'category-porn', 'category-ads-all', 'speedtest'] },
]
const grouped = new Set(groups.flatMap((group) => group.categories))
type Command = { category: ManagedRuleCategory; policy?: ManagedRulePolicy; payload?: ManagedRuleInput }
const keyFor = (item: ManagedRuleCategory) => `${item.kind}:${item.category}`

export function CategoryRules({ data, disabled = false, probes, probesLoading = false, probesError = false }: {
  data?: RulesResponse; disabled?: boolean; probes?: ServiceProbeResponse; probesLoading?: boolean; probesError?: boolean
}) {
  const client = useQueryClient()
  const [open, setOpen] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [notice, setNotice] = useState('')
  const serviceByCategory = new Map((probes?.services ?? []).map((service) => [service.category, service]))
  const policies = new Map((data?.policies ?? []).map((policy) => [keyFor(policy), policy]))
  // Preserve an existing assignment even when it has left the curated catalogue.
  const catalog = new Map((data?.policy_catalog ?? []).map((item) => [keyFor(item), item]))
  for (const policy of data?.policies ?? []) if (!catalog.has(keyFor(policy))) catalog.set(keyFor(policy), policy)
  const items = [...catalog.values()].filter((item) => `${item.label} ${item.category}`.toLocaleLowerCase().includes(search.toLocaleLowerCase().trim()))
  const mutation = useMutation({
    mutationKey: ['rules-write'],
    mutationFn: async (command: Command) => {
      try {
        const updated = command.payload
          ? command.policy ? await api.updateManagedRule(command.policy.id, command.payload) : await api.createManagedRule(command.payload)
          : await api.deleteManagedRule(command.policy!.id)
        if (command.payload && (!updated || updated.action !== command.payload.action || updated.enabled !== command.payload.enabled)) {
          throw new Error('Состояние изменилось параллельно. Требуется повторное чтение.')
        }
        client.setQueryData<RulesResponse>(['rules'], (previous) => previous && ({ ...previous, policies: [
          ...(previous.policies ?? []).filter((item) => keyFor(item) !== keyFor(command.category)),
          ...(updated ? [updated] : []),
        ] }))
        return 'Изменение сохранено. Пересекающиеся правила могут иметь более высокий приоритет.'
      } catch (error) {
        const confirmed = await reconcileRules(error, (rules) => Array.isArray(rules.policies) && !rules.section_errors?.policies && (command.payload
          ? Boolean(rules.policies?.some((item) => keyFor(item) === keyFor(command.category) && item.action === command.payload!.action && item.enabled === command.payload!.enabled))
          : !rules.policies?.some((item) => item.id === command.policy!.id)))
        client.setQueryData(['rules'], confirmed)
        return 'Ответ потерян, сохранённое состояние подтверждено. Завершение перезагрузки Mihomo этим не подтверждается.'
      }
    },
    onMutate: () => setNotice(''),
    onSuccess: (message) => { setNotice(message); void client.invalidateQueries({ queryKey: ['rules'] }) },
  })
  const busy = mutation.isPending || disabled
  const renderGroup = (title: string, description: string, entries: ManagedRuleCategory[]) => entries.length > 0 && <section className="policy-category-group" key={title}>
    <div><h3>{title}</h3><p>{description}</p></div>
    <div className="policy-category-options">{entries.map((item) => {
      const key = keyFor(item)
      const policy = policies.get(key)
      const expanded = open === key
      return <div className={`rule-tile${expanded ? ' is-open' : ''}`} key={key}>
        <button type="button" className={`policy-category-option${policy?.enabled ? ' is-active' : ''}${expanded ? ' selected' : ''}`} aria-expanded={expanded} aria-controls={`editor-${key}`} data-policy-state={policy?.enabled ? 'active' : policy ? 'disabled' : undefined} onClick={() => setOpen(expanded ? null : key)}>
          <strong>{item.label}</strong><small>{item.description || (item.kind === 'GEOSITE' ? 'Доменная категория' : 'Сеть или страна')}</small>
          <small className={policy?.enabled ? 'policy-active-badge' : ''}>{policy ? policy.enabled ? `Назначено: ${actionLabel(policy.action)}` : 'Правило отключено' : 'По общим правилам'}</small>
        </button>
        <ServiceProbeStatus service={item.kind === 'GEOSITE' ? serviceByCategory.get(item.category) : undefined} enabled={probes?.enabled} expanded={expanded} loading={probesLoading} error={probesError} />
        {expanded && <RuleEditor key={`${key}:${policy?.action}:${policy?.enabled}`} item={item} policy={policy} busy={busy} directPresent={Boolean(data?.direct_text?.trim())} onCommand={(payload) => mutation.mutate({ category: item, policy, payload })} />}
      </div>
    })}</div>
  </section>
  return <section className="panel policy-panel" aria-labelledby="policy-title">
    <div className="section-heading"><div><h2 id="policy-title">Маршрутизация GeoSite / GeoIP</h2><span>Нажмите на плитку, чтобы назначить или изменить маршрут.</span></div></div>
    <div className="rules-workspace-body">
      <ServiceProbeSummary data={probes} loading={probesLoading} error={probesError} />
      <label className="rules-search">Найти категорию<input type="search" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
      <p className="rules-hint">Плитки — каталог направлений. Наличие категории в установленном файле и фактический маршрут отдельного соединения проверяются отдельно.</p>
      <div className="policy-category-groups">
        {groups.map((group) => renderGroup(group.title, group.description, items.filter((item) => item.kind === 'GEOSITE' && group.categories.includes(item.category))))}
        {renderGroup('Популярные сайты и сервисы', 'OpenAI, Claude, Google, видео и мессенджеры.', items.filter((item) => item.kind === 'GEOSITE' && !grouped.has(item.category)))}
        {renderGroup('Сети и страны GeoIP', 'Правило по IP назначения, не проверка доступности всей страны.', items.filter((item) => item.kind === 'GEOIP'))}
      </div>
      {data && !items.length && <p>Подходящих категорий нет.</p>}
      {mutation.isPending && <p role="status">Сохраняем назначение… Не повторяйте запрос.</p>}
      {notice && <p className="success-message" role="status">{notice}</p>}
      {mutation.error && <p className="form-error" role="alert">{mutation.error.message}</p>}
    </div>
  </section>
}

function RuleEditor({ item, policy, busy, directPresent, onCommand }: { item: ManagedRuleCategory; policy?: ManagedRulePolicy; busy: boolean; directPresent: boolean; onCommand: (payload?: ManagedRuleInput) => void }) {
  const [action, setAction] = useState(policy?.action ?? 'VPS-FALLBACK')
  const [confirm, setConfirm] = useState(false)
  const payload = (enabled: boolean): ManagedRuleInput => ({ kind: item.kind, category: item.category, action, enabled })
  return <div id={`editor-${keyFor(item)}`} role="group" aria-label={`Маршрут: ${item.label}`} className="rule-tile-editor" onKeyDown={(event) => { if (event.key === 'Escape') setConfirm(false) }}>
    <label>Маршрут<select aria-label="Маршрут" value={action} disabled={busy} onChange={(event) => setAction(event.target.value as ManagedRuleInput['action'])}>{routeChoices.map((route) => <option key={route.value} value={route.value}>{route.label}</option>)}</select></label>
    <button type="button" className="primary-button" disabled={busy || Boolean(policy?.enabled && action === policy.action)} onClick={() => onCommand(payload(true))}>Применить</button>
    {policy && <button type="button" className="text-button" disabled={busy} onClick={() => onCommand({ ...payload(!policy.enabled), action: policy.action })}>{policy.enabled ? 'Отключить правило' : 'Включить правило'}</button>}
    {policy && <button type="button" className="text-button danger" disabled={busy} onClick={() => setConfirm(true)}>Убрать назначение</button>}
    {confirm && <div role="group" aria-label="Подтверждение удаления правила" className="inline-confirm"><p>Убрать назначение? Сервис будет следовать общим правилам.</p><button type="button" className="text-button danger" disabled={busy} onClick={() => onCommand()}>Удалить</button><button type="button" className="text-button" disabled={busy} onClick={() => setConfirm(false)}>Отмена</button></div>}
    <details><summary>Приоритет и сведения</summary><p>{item.kind}: {item.category}</p><p>Авто VPN использует основной и резервный выход. Конкретный VPN фиксирует только этот выход.</p><p>{directPresent ? 'Свой DIRECT-список непустой: совпавший домен из него может перекрывать это назначение.' : 'При пересечении категорий применяется первое совпавшее правило Mihomo.'}</p><p>По общим правилам — отсутствие личного назначения, а не принудительный DIRECT.</p><p>ByeDPI ещё не прошёл приёмку и недоступен для назначения.</p></details>
  </div>
}
