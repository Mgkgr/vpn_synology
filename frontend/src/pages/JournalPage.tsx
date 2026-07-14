import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { AsyncState } from '../components/AsyncState'
import { formatDate, Status } from '../components/Status'

export function JournalPage() {
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [type, setType] = useState('')
  const [outbound, setOutbound] = useState('')
  const [endpoint, setEndpoint] = useState('')
  const filters = useMemo(() => ({ from: toIsoInstant(from), to: toIsoInstant(to), action: type || undefined, outbound: outbound || undefined, endpoint: endpoint || undefined }), [from, to, type, outbound, endpoint])
  const journal = useQuery({ queryKey: ['journal', filters], queryFn: () => api.journal(filters) })
  const types = [...new Set((journal.data?.events ?? []).map((event) => event.action))]
  return <main className="page"><header className="page-header"><div><p className="eyebrow">06 / АУДИТ И ПРОБЫ</p><h1>Журнал</h1><p>Входы администраторов, изменения клиентов и правил, маршрутизация и проверки — без конфигураций и секретов.</p></div></header>
    <section className="panel filters" aria-label="Фильтры журнала"><label>С<input aria-label="Начало периода" type="datetime-local" value={from} onChange={(event) => setFrom(event.target.value)} /></label><label>По<input aria-label="Конец периода" type="datetime-local" value={to} onChange={(event) => setTo(event.target.value)} /></label><label>Тип<select value={type} onChange={(event) => setType(event.target.value)}><option value="">Все события</option>{types.map((item) => <option key={item} value={item}>{item}</option>)}</select></label><label>Выход<input value={outbound} onChange={(event) => setOutbound(event.target.value)} placeholder="WG-IMP" /></label><label>Endpoint<input value={endpoint} onChange={(event) => setEndpoint(event.target.value)} placeholder="https://…" /></label></section>
    <section className="panel"><div className="section-heading"><h2>События</h2><span>{journal.data?.events.length ?? 0}</span></div><AsyncState loading={journal.isLoading} error={journal.error} empty={!journal.data?.events.length} emptyLabel="Нет событий по выбранным фильтрам."><table className="data-table"><thead><tr><th>Время</th><th>Тип</th><th>Администратор</th><th>Событие</th><th>Выход</th><th>Endpoint</th><th>Результат</th></tr></thead><tbody>{journal.data?.events.map((event) => <tr key={`${event.kind}-${event.id}`}><td>{formatDate(event.observed_at)}</td><td>{event.kind}</td><td>{event.actor ?? 'система'}</td><th scope="row">{event.action}</th><td>{event.outbound ?? '—'}</td><td>{event.endpoint ?? '—'}</td><td><Status ok={event.succeeded} pending={event.succeeded === null} /></td></tr>)}</tbody></table></AsyncState></section>
  </main>
}

function toIsoInstant(value: string): string | undefined {
  if (!value) return undefined
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? undefined : parsed.toISOString()
}
