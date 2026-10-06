import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import type { RulesResponse } from '../../api/types'
import { reconcileRules, sameLines } from './ruleState'

type DomainRow = { index: number; domain: string; type: 'DOMAIN' | 'DOMAIN-SUFFIX' }
function domainsFrom(text: string): DomainRow[] {
  return text.split('\n').flatMap((line, index) => {
    const [type, domain, action, ...extra] = line.trim().split(',').map((part) => part.trim())
    return (type === 'DOMAIN' || type === 'DOMAIN-SUFFIX') && domain && (!action || action === 'DIRECT') && !extra.length
      ? [{ index, domain, type }] : []
  })
}
function normalizeDomain(input: string): string {
  const value = input.trim().replace(/\.$/, '')
  if (!value || /[\s/:,?#@\\]/u.test(value)) throw new Error('Введите только доменное имя, без адреса страницы и пробелов.')
  const host = new URL(`https://${value}`).hostname
  if (!host.includes('.') || host.length > 253 || !host.split('.').every((part) => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(part)) || /^[\d.]+$/.test(host)) throw new Error('Нужно доменное имя сайта. IP-правила доступны в техническом режиме.')
  return host
}

export function DirectRulesEditor({ serverText, serverHash, error, disabled = false }: { serverText?: string | null; serverHash?: string | null; error?: string; disabled?: boolean }) {
  const client = useQueryClient()
  const [draft, setDraft] = useState<{ base: string; text: string; hash?: string } | null>(null)
  const [search, setSearch] = useState('')
  const [limit, setLimit] = useState(12)
  const [site, setSite] = useState('')
  const [includeChildren, setIncludeChildren] = useState(true)
  const [editing, setEditing] = useState<DomainRow | null>(null)
  const [deleteIndex, setDeleteIndex] = useState<number | null>(null)
  const [technical, setTechnical] = useState(false)
  const [notice, setNotice] = useState('')
  const [validation, setValidation] = useState('')
  const text = draft?.text ?? serverText ?? ''
  const loaded = typeof serverText === 'string' && !error
  const conflict = Boolean(draft && loaded && !sameLines(draft.base, serverText!))
  const changed = Boolean(draft && !sameLines(draft.text, draft.base))
  const rows = domainsFrom(text)
  const visible = rows.filter((row) => row.domain.toLowerCase().includes(search.toLowerCase().trim()))
  const domainIndexes = new Set(rows.map((row) => row.index))
  const technicalCount = text.split('\n').filter((line, index) => line.trim() && !line.trim().startsWith('#') && !domainIndexes.has(index)).length
  const pinDraft = () => setDraft((previous) => previous ?? { base: serverText ?? '', hash: serverHash ?? undefined, text })
  const change = (value: string) => { setDraft((previous) => ({ base: previous?.base ?? serverText ?? '', hash: previous?.hash ?? serverHash ?? undefined, text: value })); setNotice(''); setValidation('') }
  const mutation = useMutation({
    mutationKey: ['rules-write'],
    mutationFn: async (attempt: { text: string; base: string; hash?: string }) => {
      // Early feedback from GET, then an atomic revision check on the server.
      const fresh = await api.rules()
      client.setQueryData(['rules'], fresh)
      if (!sameLines(fresh.direct_text, attempt.base)) throw new Error('На сервере список изменился. Черновик сохранён; сначала сверьте изменения.')
      try {
        const revision = await api.applyDirectRules(attempt.text, attempt.hash)
        client.setQueryData<RulesResponse>(['rules'], (previous) => previous && ({ ...previous, direct_text: attempt.text }))
        return `DIRECT-правила сохранены. Ревизия #${revision.revision_number}.`
      } catch (failure) {
        const confirmed = await reconcileRules(failure, (rules) => sameLines(rules.direct_text, attempt.text))
        client.setQueryData(['rules'], confirmed)
        return 'Ответ потерян, сохранённое состояние подтверждено. Завершение перезагрузки Mihomo этим не подтверждается.'
      }
    },
    onMutate: () => setNotice(''),
    onSuccess: (message) => { setNotice(message); setDraft(null); void client.invalidateQueries({ queryKey: ['rules'] }) },
  })
  const busy = disabled || mutation.isPending || !loaded
  const saveSite = () => {
    try {
      const domain = normalizeDomain(site)
      const type = includeChildren ? 'DOMAIN-SUFFIX' : 'DOMAIN'
      if (rows.some((row) => row.index !== editing?.index && row.domain === domain && row.type === type)) throw new Error('Такой сайт с этим типом правила уже есть.')
      const line = `${type},${domain},DIRECT`
      if (editing) {
        const lines = text.split('\n'); lines[editing.index] = line; change(lines.join('\n'))
      } else change(`${text}${text && !text.endsWith('\n') ? '\n' : ''}${line}\n`)
      setEditing(null); setSite(''); setIncludeChildren(true)
    } catch (failure) { setValidation(failure instanceof Error ? failure.message : 'Не удалось разобрать домен.') }
  }
  return <section className="panel editor-panel" aria-labelledby="direct-title">
    <div className="section-heading"><h2 id="direct-title">Свой DIRECT-список</h2><span>{loaded || draft ? `${rows.length} сайтов${!loaded ? ' в черновике' : ''}` : error || serverText === null ? 'Список недоступен' : 'Загрузка списка…'}</span></div>
    <div className="rules-workspace-body">
      {error && <p role="alert" className="form-error">{error}</p>}
      <label className="rules-search">Найти сайт<input type="search" value={search} onChange={(event) => { setSearch(event.target.value); setLimit(12) }} /></label>
      <ul className="direct-sites">{visible.slice(0, limit).map((row) => <li key={row.index}>
        <button type="button" className="text-button direct-domain" title={row.type === 'DOMAIN' ? 'Только точный домен' : 'Домен и поддомены'} disabled={busy} onClick={() => { pinDraft(); setEditing(row); setSite(row.domain); setIncludeChildren(row.type === 'DOMAIN-SUFFIX') }}>{row.domain}</button>
        <button type="button" className="text-button danger" aria-label={`Удалить ${row.domain}`} disabled={busy} onClick={() => { pinDraft(); setDeleteIndex(row.index) }}>×</button>
        {deleteIndex === row.index && <div className="inline-confirm" role="group" aria-label={`Удаление ${row.domain}`}><span>Убрать сайт из черновика?</span><button type="button" className="text-button danger" disabled={busy} onClick={() => { change(text.split('\n').filter((_, index) => index !== row.index).join('\n')); setDeleteIndex(null); setEditing(null); setSite('') }}>Удалить</button><button type="button" className="text-button" onClick={() => setDeleteIndex(null)}>Отмена</button></div>}
      </li>)}</ul>
      {visible.length > limit && <button type="button" className="text-button" onClick={() => setLimit(limit + 12)}>Показать ещё ({visible.length - limit})</button>}
      {loaded && !visible.length && <p className="rules-hint">Сайтов по этому запросу нет.</p>}
      <form className="direct-add" onSubmit={(event) => { event.preventDefault(); saveSite() }}>
        <label>{editing ? 'Изменить сайт' : 'Новый сайт DIRECT'}<input aria-label="Новый сайт DIRECT" value={site} disabled={busy} placeholder="example.org" onChange={(event) => setSite(event.target.value)} /></label>
        <label className="inline-checkbox"><input type="checkbox" checked={includeChildren} disabled={busy} onChange={(event) => setIncludeChildren(event.target.checked)} />Включая поддомены</label>
        <button type="submit" className="text-button" disabled={busy || !site.trim()}>{editing ? 'Изменить сайт' : 'Добавить сайт'}</button>
        {editing && <button type="button" className="text-button" onClick={() => { setEditing(null); setSite(''); setIncludeChildren(true) }}>Отмена редактирования</button>}
      </form>
      {validation && <p role="alert" className="form-error">{validation}</p>}
      <details className="rules-details" onToggle={(event) => setTechnical(event.currentTarget.open)}><summary>Технический режим · прочих правил: {technicalCount}</summary>{technical && <><p className="rules-hint">IP-правила и точные домены сохраняются. Здесь можно вставить полный список для импорта; перед применением проверьте изменения.</p><label htmlFor="direct-rules">Правила DIRECT</label><textarea id="direct-rules" value={text} disabled={busy} onChange={(event) => { setEditing(null); change(event.target.value) }} /></>}</details>
      {changed && <details className="rules-details"><summary>Предпросмотр изменений</summary><div className="diff-grid"><div><h3>Добавится</h3><ul>{text.split('\n').filter((line) => line.trim() && !draft!.base.split('\n').includes(line)).map((line, index) => <li key={index}>+ {line}</li>)}</ul></div><div><h3>Будет удалено</h3><ul>{draft!.base.split('\n').filter((line) => line.trim() && !text.split('\n').includes(line)).map((line, index) => <li key={index}>− {line}</li>)}</ul></div></div></details>}
      {conflict && <p role="alert" className="form-error">На сервере список изменился. Черновик сохранён; сверьте изменения в техническом режиме или сбросьте черновик.</p>}
      {loaded && notice && <p className="success-message" role="status">{notice}</p>}
      {mutation.error && !notice && <p role="alert" className="form-error">{mutation.error.message}</p>}
      <div className="rules-actions"><button type="button" className="primary-button" disabled={busy || !changed || conflict} onClick={() => mutation.mutate({ text, base: draft!.base, hash: draft!.hash })}>{mutation.isPending ? 'Сохраняем…' : 'Применить DIRECT-правила'}</button>{draft && <button className="text-button" type="button" disabled={mutation.isPending} onClick={() => { setDraft(null); setEditing(null); setSite(''); setNotice('') }}>Сбросить черновик</button>}</div>
    </div>
  </section>
}
