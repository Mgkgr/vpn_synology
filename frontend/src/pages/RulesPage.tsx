import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import type { GeoUpdate, ManagedRuleInput, ManagedRulePolicy } from '../api/types'
import { AsyncState } from '../components/AsyncState'

type PolicyKind = 'GEOSITE' | 'GEOIP'
type PolicyCategory = { kind: PolicyKind, category: string, label: string, description?: string }

function policyKey(kind: PolicyKind, category: string): string {
  return `${kind}:${category}`
}

const routeActions: { value: ManagedRuleInput['action'], title: string, description: string }[] = [
  { value: 'VPS-FALLBACK', title: 'Авто VPN', description: 'WG-IMP → HY2-USA' },
  { value: 'WG-IMP', title: 'Только WG-IMP', description: 'основной выход' },
  { value: 'HY2-USA', title: 'Только HY2-USA', description: 'резервный выход' },
  { value: 'DIRECT', title: 'DIRECT', description: 'в обход VPN' },
]

const russianGeoSiteCategories = new Set([
  'category-bank-ru', 'category-ecommerce-ru', 'category-retail-ru', 'category-gov-ru',
  'category-entertainment-ru', 'category-media-ru', 'category-travel-ru', 'category-medicine-ru',
  'category-ru', 'ozon', 'wildberries', 'avito', 'sber', 'tbank-ru', 'yandex', 'kinopoisk', 'vk',
  'mailru-group', 'cdek', 'megafon', 'mts-ru', 'rostelecom', 't2-ru',
])

const p2pGeoSiteCategories = new Set([
  'category-public-tracker', 'category-pt', 'tracker',
])

const streamingGeoSiteCategories = new Set([
  'category-entertainment', 'category-media', 'disney', 'hbo', 'primevideo', 'twitch', 'dazn', 'bilibili', 'biliintl',
])

const gamesGeoSiteCategories = new Set([
  'category-games', 'category-game-platforms-download', 'category-android-app-download', 'steam',
])

const specialGeoSiteCategories = new Set([
  'anime', 'ehentai', 'category-porn', 'category-ads-all', 'speedtest',
])

const groupedGeoSiteCategories = new Set([
  ...russianGeoSiteCategories,
  ...p2pGeoSiteCategories,
  ...streamingGeoSiteCategories,
  ...gamesGeoSiteCategories,
  ...specialGeoSiteCategories,
])

function normalizedLines(value: string): string[] {
  return value.split('\n').map((line) => line.trim()).filter(Boolean)
}

function sameNormalizedLines(left: string, right: string): boolean {
  const leftLines = normalizedLines(left)
  const rightLines = normalizedLines(right)
  return leftLines.length === rightLines.length && leftLines.every((line, index) => line === rightLines[index])
}

function RouteActions() {
  return <>{routeActions.map((item) => <option value={item.value} key={item.value}>{item.title} — {item.description}</option>)}</>
}

function activePolicyCaption(policy: ManagedRulePolicy): string {
  const action = routeActions.find((item) => item.value === policy.action)
  return `Уже активно: ${action?.title ?? policy.action}`
}

function RouteActionPicker({ value, onChange }: { value: ManagedRuleInput['action'], onChange: (value: ManagedRuleInput['action']) => void }) {
  return <div className="policy-route-options" aria-label="Маршрут для нового правила">
    {routeActions.map((item) => <button className={`policy-route-option ${value === item.value ? 'selected' : ''}`} type="button" key={item.value} aria-pressed={value === item.value} onClick={() => onChange(item.value)}><strong>{item.title}</strong><small>{item.description}</small></button>)}
  </div>
}

function CategoryPicker({ catalog, kind, category, onPick }: { catalog: PolicyCategory[], kind: PolicyKind, category: string, onPick: (item: PolicyCategory) => void }) {
  const geosite = catalog.filter((item) => item.kind === 'GEOSITE')
  const geoip = catalog.filter((item) => item.kind === 'GEOIP')
  const effectiveCategory = catalog.some((item) => item.kind === kind && item.category === category)
    ? category
    : catalog.find((item) => item.kind === kind)?.category ?? ''
  // Same query key as RulesPage: React Query shares the request and cached data.
  const activePoliciesResponse = useQuery({ queryKey: ['rules'], queryFn: api.rules })
  const activePolicies = new Map((activePoliciesResponse.data?.policies ?? []).filter((item) => item.enabled).map((item) => [policyKey(item.kind, item.category), item]))
  const inGroup = (categories: Set<string>) => geosite.filter((item) => categories.has(item.category))
  const russian = inGroup(russianGeoSiteCategories)
  const p2p = inGroup(p2pGeoSiteCategories)
  const streaming = inGroup(streamingGeoSiteCategories)
  const games = inGroup(gamesGeoSiteCategories)
  const special = inGroup(specialGeoSiteCategories)
  const popular = geosite.filter((item) => !groupedGeoSiteCategories.has(item.category))
  const group = (title: string, description: string, items: PolicyCategory[]) => <div className="policy-category-group"><div><h3>{title}</h3><p>{description}</p></div><div className="policy-category-options">{items.map((item) => {
    const activePolicy = activePolicies.get(policyKey(item.kind, item.category))
    const selected = kind === item.kind && effectiveCategory === item.category
    return <button type="button" className={`policy-category-option${activePolicy ? ' is-active' : ''}${selected ? ' selected' : ''}`} key={`${item.kind}-${item.category}`} aria-pressed={selected} data-policy-state={activePolicy ? 'active' : undefined} onClick={() => onPick(item)}><strong>{item.label}</strong><small>{item.description || (item.kind === 'GEOSITE' ? 'доменная категория' : 'сеть или страна')}</small>{activePolicy && <small className="policy-active-badge">{activePolicyCaption(activePolicy)}</small>}</button>
  })}</div></div>
  return <div className="policy-category-groups">
    {russian.length > 0 && group('Российские сервисы', 'Банки, маркетплейсы, госуслуги и инфраструктура. Широкое правило направит весь набор российских доменов, поэтому для банков и магазинов лучше выбрать точную категорию.', russian)}
    {p2p.length > 0 && group('P2P / торренты', 'Только домены трекеров. Это не анализ и не перехват всего BitTorrent-трафика.', p2p)}
    {streaming.length > 0 && group('Видео и стриминг', 'Широкие категории могут включать легальные сервисы, медиа и общую инфраструктуру. Для узкого маршрута выбирайте точный сервис.', streaming)}
    {games.length > 0 && group('Игры и загрузки', 'Игровые платформы, обновления и источники загрузок; широкие правила могут затронуть CDN.', games)}
    {special.length > 0 && group('Инфраструктура и специальные категории', 'Тематические и инфраструктурные наборы. Они задают маршрут, но не блокируют домены.', special)}
    {popular.length > 0 && group('Популярные сайты и сервисы', 'OpenAI, Claude, Google, видео и мессенджеры.', popular)}
    {geoip.length > 0 && group('Сети и страны GeoIP', 'Правила для IP-адресов, CDN и регионов.', geoip)}
  </div>
}

function PolicyRow({
  policy,
  catalog,
  pending,
  onSave,
  onDelete,
}: {
  policy: ManagedRulePolicy
  catalog: PolicyCategory[]
  pending: boolean
  onSave: (id: number, payload: ManagedRuleInput) => void
  onDelete: (id: number) => void
}) {
  const [kind, setKind] = useState(policy.kind)
  const [category, setCategory] = useState(policy.category)
  const [action, setAction] = useState(policy.action)
  const [enabled, setEnabled] = useState(policy.enabled)
  const options = catalog.filter((item) => item.kind === kind)
  const validCategory = options.some((item) => item.category === category) ? category : options[0]?.category ?? ''
  return <tr><th scope="row"><select aria-label={`Тип правила ${policy.id}`} value={kind} onChange={(event) => { setKind(event.target.value as PolicyKind); setCategory(catalog.find((item) => item.kind === event.target.value)?.category ?? '') }} disabled={pending}><option value="GEOSITE">GeoSite</option><option value="GEOIP">GeoIP</option></select><select aria-label={`Категория правила ${policy.id}`} value={validCategory} onChange={(event) => setCategory(event.target.value)} disabled={pending}>{options.map((item) => <option key={item.category} value={item.category}>{item.label}</option>)}</select></th><td><select aria-label={`Маршрут правила ${policy.id}`} value={action} onChange={(event) => setAction(event.target.value as ManagedRuleInput['action'])} disabled={pending}><RouteActions /></select></td><td><input aria-label={`Активировать правило ${policy.id}`} type="checkbox" checked={enabled} disabled={pending} onChange={(event) => setEnabled(event.target.checked)} /></td><td className="actions"><button className="text-button" type="button" disabled={pending || !validCategory} onClick={() => onSave(policy.id, { kind, category: validCategory, action, enabled })}>Сохранить</button><button className="text-button danger" type="button" disabled={pending} onClick={() => { if (window.confirm(`Удалить правило «${policy.label}»?`)) onDelete(policy.id) }}>Удалить</button></td></tr>
}

export function RulesPage() {
  const [text, setText] = useState('')
  const [loadedText, setLoadedText] = useState<string | null>(null)
  const [newKind, setNewKind] = useState<PolicyKind>('GEOSITE')
  const [newCategory, setNewCategory] = useState('openai')
  const [newAction, setNewAction] = useState<ManagedRuleInput['action']>('VPS-FALLBACK')
  const [geoPhase, setGeoPhase] = useState<'idle' | 'request' | 'verify'>('idle')
  const [policyNotice, setPolicyNotice] = useState<string | null>(null)
  const [directNotice, setDirectNotice] = useState<string | null>(null)
  const queryClient = useQueryClient()
  const rules = useQuery({ queryKey: ['rules'], queryFn: api.rules })
  const apply = useMutation({
    mutationFn: api.applyDirectRules,
    onMutate: () => setDirectNotice(null),
    onSuccess: () => {
      setLoadedText(text)
      void queryClient.invalidateQueries({ queryKey: ['rules'] })
    },
    onError: async (_error, attemptedText) => {
      // Mihomo may reload after writing the provider file, which can interrupt
      // the browser request after the server has already committed the revision.
      await new Promise((resolve) => window.setTimeout(resolve, 1200))
      try {
        await queryClient.invalidateQueries({ queryKey: ['rules'] })
        const refreshed = await queryClient.fetchQuery({ queryKey: ['rules'], queryFn: api.rules })
        if (sameNormalizedLines(refreshed.direct_text, attemptedText)) {
          setLoadedText(refreshed.direct_text)
          setText(refreshed.direct_text)
          setDirectNotice('DIRECT-правила применены. Браузер потерял ответ при перезагрузке Mihomo, но состояние подтверждено.')
        }
      } catch {
        // Keep the original mutation error when the confirming read also fails.
      }
    },
  })
  const createPolicy = useMutation({
    mutationFn: api.createManagedRule,
    onMutate: () => setPolicyNotice(null),
    onSuccess: () => {
      setPolicyNotice('Правило добавлено и применено.')
      void queryClient.invalidateQueries({ queryKey: ['rules'] })
    },
    onError: async (_error, payload) => {
      // A Mihomo reload can briefly interrupt the browser connection after the
      // server already committed the policy. Re-read the source of truth once
      // before presenting a failure to the administrator.
      await new Promise((resolve) => window.setTimeout(resolve, 1200))
      try {
        await queryClient.invalidateQueries({ queryKey: ['rules'] })
        const refreshed = await queryClient.fetchQuery({ queryKey: ['rules'], queryFn: api.rules })
        const exists = refreshed.policies?.some((item) => item.kind === payload.kind && item.category === payload.category && item.action === payload.action && item.enabled === payload.enabled)
        if (exists) {
          setPolicyNotice('Правило применено. Браузер потерял ответ при перезагрузке Mihomo, но состояние подтверждено.')
        }
      } catch {
        // Keep the original mutation error when the confirming read also fails.
      }
    },
  })
  const updatePolicy = useMutation({ mutationFn: ({ id, payload }: { id: number, payload: ManagedRuleInput }) => api.updateManagedRule(id, payload), onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['rules'] }) })
  const deletePolicy = useMutation({ mutationFn: api.deleteManagedRule, onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['rules'] }) })
  const upgradeGeo = useMutation({
    mutationFn: async () => {
      setGeoPhase('request')
      const timer = window.setTimeout(() => setGeoPhase('verify'), 800)
      try {
        return await api.updateGeo()
      } finally {
        window.clearTimeout(timer)
        setGeoPhase('idle')
      }
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['rules'] })
      void queryClient.invalidateQueries({ queryKey: ['updates'] })
    },
  })
  useEffect(() => {
    if (rules.data !== undefined) {
      setLoadedText(rules.data.direct_text)
      setText(rules.data.direct_text)
    }
  }, [rules.data])
  const currentDirect = useMemo(() => normalizedLines(loadedText ?? ''), [loadedText])
  const draftLines = normalizedLines(text)
  const additions = draftLines.filter((line) => !currentDirect.includes(line))
  const removals = currentDirect.filter((line) => !draftLines.includes(line))
  const canApply = loadedText !== null && text !== loadedText && !apply.isPending
  const policyCatalog = rules.data?.policy_catalog ?? []
  const newCategoryOptions = policyCatalog.filter((item) => item.kind === newKind)
  useEffect(() => {
    if (!newCategoryOptions.some((item) => item.category === newCategory)) setNewCategory(newCategoryOptions[0]?.category ?? '')
  }, [newKind, newCategory, newCategoryOptions])

  return <main className="page"><header className="page-header"><div><p className="eyebrow">04 / ПРАВИЛА MIHOMO</p><h1>Правила</h1><p>Изменения DIRECT проходят предварительный просмотр до применения.</p>{rules.isLoading && <p className="loading-hint" role="status">Загружаем GeoSite, GeoIP и правила Mihomo…</p>}</div><div><button className="primary-button" type="button" onClick={() => upgradeGeo.mutate()} disabled={upgradeGeo.isPending}>{geoPhase === 'request' ? 'Отправляем запрос Mihomo…' : geoPhase === 'verify' ? 'Проверяем GeoSite / GeoIP…' : 'Обновить GeoSite / GeoIP'}</button>{upgradeGeo.isPending && <p className="loading-hint" role="status">{geoPhase === 'verify' ? 'Mihomo ответил, сверяем файлы GeoData на шлюзе…' : 'Запускаем обновление GeoData в Mihomo…'}</p>}{upgradeGeo.error && <p className="form-error" role="alert">{upgradeGeo.error.message}</p>}{upgradeGeo.isSuccess && <p className="success-message">{geoUpdateSummary(upgradeGeo.data)}</p>}</div></header>
    <section className="panel policy-panel" aria-labelledby="policy-title"><div className="section-heading"><div><h2 id="policy-title">Маршрутизация GeoSite / GeoIP</h2><span>Выберите направление, назначьте ему маршрут и добавьте правило.</span></div></div><div className="policy-composer"><section className="policy-step" aria-labelledby="policy-category-title"><div className="policy-step-heading"><span>1</span><div><h3 id="policy-category-title">Куда направлять</h3><p>Готовые направления из активных списков MetaCubeX.</p></div></div><CategoryPicker catalog={policyCatalog} kind={newKind} category={newCategory} onPick={(item) => { setNewKind(item.kind); setNewCategory(item.category) }} /></section><section className="policy-step" aria-labelledby="policy-action-title"><div className="policy-step-heading"><span>2</span><div><h3 id="policy-action-title">Как направлять</h3><p>Выберите автоматический VPN, конкретный выход или DIRECT.</p></div></div><RouteActionPicker value={newAction} onChange={setNewAction} /></section><div className="policy-advanced"><label>Расширенный выбор <select aria-label="Тип нового правила" value={newKind} onChange={(event) => setNewKind(event.target.value as PolicyKind)}><option value="GEOSITE">GeoSite</option><option value="GEOIP">GeoIP</option></select><select aria-label="Категория нового правила" value={newCategory} onChange={(event) => setNewCategory(event.target.value)}>{newCategoryOptions.map((item) => <option value={item.category} key={`${item.kind}-${item.category}`}>{item.label}</option>)}</select></label><button className="primary-button" type="button" disabled={!newCategory || createPolicy.isPending} onClick={() => createPolicy.mutate({ kind: newKind, category: newCategory, action: newAction, enabled: true })}>{createPolicy.isPending ? 'Добавление…' : 'Добавить правило'}</button></div></div>{policyNotice && <p className="success-message" role="status">{policyNotice}</p>}{createPolicy.error && !policyNotice && <p className="form-error policy-error" role="alert">{createPolicy.error.message}</p>}<div className="policy-table-wrap"><AsyncState loading={rules.isLoading} error={rules.error} empty={!(rules.data?.policies?.length)} emptyLabel="Категорий пока нет. Добавьте нужный маршрут выше."><table className="data-table"><thead><tr><th>Категория</th><th>Маршрут</th><th>Активно</th><th><span className="sr-only">Действия</span></th></tr></thead><tbody>{(rules.data?.policies ?? []).map((policy) => <PolicyRow key={policy.id} policy={policy} catalog={policyCatalog} pending={updatePolicy.isPending || deletePolicy.isPending} onSave={(id, payload) => updatePolicy.mutate({ id, payload })} onDelete={(id) => deletePolicy.mutate(id)} />)}</tbody></table></AsyncState></div></section>
    <div className="rules-layout">
      <section className="panel editor-panel" aria-labelledby="direct-title"><div className="section-heading"><h2 id="direct-title">Свой DIRECT-список</h2><span>по одному правилу в строке</span></div><label className="sr-only" htmlFor="direct-rules">Правила DIRECT</label><textarea id="direct-rules" value={text} onChange={(event) => setText(event.target.value)} placeholder="DOMAIN-SUFFIX,example.local,DIRECT\nIP-CIDR,192.168.0.0/16,DIRECT" disabled={loadedText === null} /><div className="diff-grid"><div><h3>Добавится</h3>{additions.length ? <ul>{additions.map((line) => <li key={line}>+ {line}</li>)}</ul> : <p>Нет изменений.</p>}</div><div><h3>Будет удалено</h3>{removals.length ? <ul>{removals.map((line) => <li key={line}>− {line}</li>)}</ul> : <p>Нет изменений.</p>}</div></div>{directNotice && <p className="success-message" role="status">{directNotice}</p>}{apply.error && !directNotice && <p className="form-error" role="alert">{apply.error.message}</p>}{apply.data && <p className="success-message">Применена ревизия #{apply.data.revision_number}.</p>}<button className="primary-button" type="button" disabled={!canApply} onClick={() => apply.mutate(text)}>{apply.isPending ? 'Применение…' : 'Применить DIRECT-правила'}</button></section>
      <section className="panel" aria-labelledby="providers-title"><div className="section-heading"><h2 id="providers-title">Rule providers</h2><span>активные источники</span></div><AsyncState loading={rules.isLoading} error={rules.error} empty={!rules.data?.providers.length} emptyLabel="Провайдеры не вернули данных.">{rules.data && <table className="data-table"><thead><tr><th>Имя</th><th>Поведение</th></tr></thead><tbody>{rules.data.providers.map((provider) => <tr key={provider.name}><th scope="row">{provider.name}</th><td>{provider.behavior}</td></tr>)}</tbody></table>}</AsyncState></section>
    </div>
    <section className="panel" aria-labelledby="active-rules-title"><div className="section-heading"><h2 id="active-rules-title">Активные правила контроллера</h2><span>только фактически загруженные строки</span></div><AsyncState loading={rules.isLoading} error={rules.error} empty={!rules.data?.rules.length} emptyLabel="Контроллер не вернул правил.">{rules.data && <table className="data-table"><thead><tr><th>Тип</th><th>Payload</th><th>Выход</th></tr></thead><tbody>{rules.data.rules.map((rule, index) => <tr key={`${rule.type}-${rule.payload}-${index}`}><td>{rule.type}</td><th scope="row">{rule.payload}</th><td>{rule.proxy}</td></tr>)}</tbody></table>}</AsyncState></section>
  </main>
}

function geoUpdateSummary(update: GeoUpdate | undefined): string {
  if (!update) return 'Запрос принят. Mihomo завершит обновление GeoData, а результат появится в истории.'
  if (update.verification === 'changed') return `Готово: изменены ${update.changed_files.join(', ')}. HTTP ${update.status_code ?? '—'}.`
  if (update.verification === 'unchanged') return `Готово: ${update.checked_files.join(', ') || 'GeoData'} уже актуальны. HTTP ${update.status_code ?? '—'}.`
  if (update.verification === 'unavailable') return `Mihomo ответил HTTP ${update.status_code ?? '—'}, но файлы GeoData после обновления недоступны для проверки.`
  return 'Обновление завершилось с ошибкой; прежние GeoData сохранены.'
}
