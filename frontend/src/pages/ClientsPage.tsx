import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import type { Client, Period } from '../api/types'
import { AsyncState } from '../components/AsyncState'
import { formatBytes, formatDate, formatElapsed } from '../components/Status'

type ClientRef = { id: number; name: string }
const ACTIVE_HANDSHAKE_WINDOW_MS = 5 * 60_000

export function ClientsPage() {
  const [dialogOpen, setDialogOpen] = useState(false)
  const [qrClient, setQrClient] = useState<ClientRef | null>(null)
  const [renameClient, setRenameClient] = useState<ClientRef | null>(null)
  const [clientToDelete, setClientToDelete] = useState<ClientRef | null>(null)
  const [credentialsDialogOpen, setCredentialsDialogOpen] = useState(false)
  const [credentialsSubmitting, setCredentialsSubmitting] = useState(false)
  const [credentialsError, setCredentialsError] = useState<Error | null>(null)
  const [period, setPeriod] = useState<Period>('month')
  const queryClient = useQueryClient()
  const clients = useQuery({ queryKey: ['clients'], queryFn: api.clients, refetchInterval: 60_000 })
  const credentialStatus = useQuery({ queryKey: ['wgeasy-credentials'], queryFn: api.wgeasyCredentialStatus })
  const usage = useQuery({ queryKey: ['traffic-usage', period], queryFn: () => api.trafficUsage(period) })
  const patchClients = (patch: (items: Client[]) => Client[]) => queryClient.setQueryData<Client[]>(['clients'], (items) => items ? patch(items) : items)
  const refreshClientData = () => Promise.all([queryClient.invalidateQueries({ queryKey: ['clients'] }), queryClient.invalidateQueries({ queryKey: ['traffic-usage'] })])
  const create = useMutation({ mutationFn: api.createClient, onSuccess: (created) => { patchClients((items) => [...items, created]); setDialogOpen(false) } })
  const disable = useMutation({ mutationFn: api.disableClient, onMutate: (id) => { const previous = queryClient.getQueryData<Client[]>(['clients']); patchClients((items) => items.map((item) => item.id === id ? { ...item, enabled: false } : item)); return previous }, onError: (_error, _id, previous) => queryClient.setQueryData(['clients'], previous), onSettled: () => void refreshClientData() })
  const enable = useMutation({ mutationFn: api.enableClient, onMutate: (id) => { const previous = queryClient.getQueryData<Client[]>(['clients']); patchClients((items) => items.map((item) => item.id === id ? { ...item, enabled: true } : item)); return previous }, onError: (_error, _id, previous) => queryClient.setQueryData(['clients'], previous), onSettled: () => void refreshClientData() })
  const rename = useMutation({ mutationFn: ({ clientId, name }: { clientId: number; name: string }) => api.renameClient(clientId, name), onSuccess: (updated) => { patchClients((items) => items.map((item) => item.id === updated.id ? updated : item)); setRenameClient(null) }, onSettled: () => void refreshClientData() })
  const remove = useMutation({ mutationFn: api.deleteClient, onMutate: (id) => { const previous = queryClient.getQueryData<Client[]>(['clients']); patchClients((items) => items.filter((item) => item.id !== id)); return previous }, onError: (_error, _id, previous) => queryClient.setQueryData(['clients'], previous), onSuccess: () => setClientToDelete(null), onSettled: () => void refreshClientData() })
  const usageByPeer = useMemo(() => new Map((usage.data?.usage ?? []).map((item) => [item.peer_id, item])), [usage.data])
  const pending = disable.isPending || enable.isPending || rename.isPending || remove.isPending

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const name = String(new FormData(event.currentTarget).get('name') ?? '').trim()
    if (name) create.mutate(name)
  }
  function submitRename(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const name = String(new FormData(event.currentTarget).get('name') ?? '').trim()
    if (renameClient && name) rename.mutate({ clientId: renameClient.id, name })
  }
  async function submitCredentials(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const username = String(form.get('username') ?? '').trim()
    const password = String(form.get('password') ?? '').trim()
    if (!username || !password) return
    setCredentialsError(null); setCredentialsSubmitting(true)
    try {
      await api.configureWgEasy(username, password)
      await Promise.all([queryClient.invalidateQueries({ queryKey: ['wgeasy-credentials'] }), queryClient.invalidateQueries({ queryKey: ['clients'] })])
      setCredentialsDialogOpen(false)
    } catch {
      setCredentialsError(new Error('Операция не выполнена. Попробуйте ещё раз.'))
    } finally { setCredentialsSubmitting(false) }
  }

  return <main className="page"><header className="page-header"><div><p className="eyebrow">03 / ПРОФИЛИ WIREGUARD</p><h1>Клиенты</h1><p>Создавайте, переименовывайте и отзывайте профили через защищённый API.</p></div><div className="actions"><span role="status">{credentialStatus.isLoading ? 'Проверяем wg-easy…' : credentialStatus.data?.configured ? 'Учётные данные wg-easy настроены' : 'Учётные данные wg-easy не настроены'}</span><button className="secondary-button" type="button" onClick={() => setCredentialsDialogOpen(true)}>{credentialStatus.data?.configured ? 'Заменить wg-easy' : 'Настроить wg-easy'}</button><button className="primary-button" type="button" onClick={() => setDialogOpen(true)}>+ Новый профиль</button></div></header>
    <section className="panel"><div className="section-heading"><h2>Профили</h2><div className="period-toggle" role="group" aria-label="Период использования"><button type="button" className={period === 'month' ? 'active' : ''} onClick={() => setPeriod('month')}>Месяц</button><button type="button" className={period === 'year' ? 'active' : ''} onClick={() => setPeriod('year')}>Год</button></div></div><AsyncState loading={clients.isLoading || usage.isLoading} error={clients.error ?? usage.error} empty={!clients.data?.length} emptyLabel="Профилей ещё нет."><table className="data-table"><thead><tr><th>Имя</th><th>Адрес</th><th>Последний handshake</th><th>Использование за {period === 'month' ? 'месяц' : 'год'}</th><th>Статус</th><th><span className="sr-only">Действия</span></th></tr></thead><tbody>{clients.data?.map((client) => { const accounted = usageByPeer.get(String(client.id)); const traffic = accounted ? accounted.received_bytes + accounted.transmitted_bytes : 0; return <tr key={client.id}><th scope="row">{client.name}</th><td>{client.ipv4_address}</td><td><HandshakeTime value={client.latest_handshake_at} /></td><td>{formatBytes(traffic)}</td><td><ClientConnectionStatus client={client} /></td><td className="actions"><button type="button" className="text-button" aria-label={`Показать QR-код ${client.name}`} onClick={() => setQrClient({ id: client.id, name: client.name })}>QR</button><a href={api.configUrl(client.id)} download={`${client.name}.conf`} aria-label={`Скачать конфиг ${client.name}`}>Конфиг</a><button type="button" className="text-button" disabled={pending} aria-label={`Переименовать ${client.name}`} onClick={() => setRenameClient({ id: client.id, name: client.name })}>Переименовать</button>{client.enabled ? <button type="button" className="text-button" disabled={pending} aria-label={`Отключить ${client.name}`} onClick={() => disable.mutate(client.id)}>Отключить</button> : <button type="button" className="text-button" disabled={pending} aria-label={`Включить ${client.name}`} onClick={() => enable.mutate(client.id)}>Включить</button>}<button type="button" className="text-button danger" disabled={pending} aria-label={`Удалить ${client.name}`} onClick={() => setClientToDelete({ id: client.id, name: client.name })}>Удалить</button></td></tr> })}</tbody></table></AsyncState></section>
    {clientToDelete && <Dialog title={`Удалить профиль ${clientToDelete.name}`} onClose={() => setClientToDelete(null)}><p>Конфигурация этого клиента будет отозвана и восстановить её нельзя.</p>{remove.error && <p className="form-error" role="alert">{remove.error.message}</p>}<div className="form-actions"><button type="button" className="secondary-button" onClick={() => setClientToDelete(null)}>Отмена</button><button type="button" className="primary-button danger" onClick={() => remove.mutate(clientToDelete.id)} disabled={remove.isPending}>{remove.isPending ? 'Удаление…' : 'Удалить навсегда'}</button></div></Dialog>}
    {dialogOpen && <Dialog title="Создать клиента" onClose={() => setDialogOpen(false)}><form onSubmit={submit}><label>Имя профиля<input name="name" required minLength={1} maxLength={255} autoFocus /></label>{create.error && <p className="form-error" role="alert">{create.error.message}</p>}<div className="form-actions"><button type="button" className="secondary-button" onClick={() => setDialogOpen(false)}>Отмена</button><button className="primary-button" type="submit" disabled={create.isPending}>{create.isPending ? 'Создание…' : 'Создать'}</button></div></form></Dialog>}
    {renameClient && <Dialog title="Переименовать профиль" onClose={() => setRenameClient(null)}><form onSubmit={submitRename}><label>Имя профиля<input name="name" defaultValue={renameClient.name} required minLength={1} maxLength={255} autoFocus /></label>{rename.error && <p className="form-error" role="alert">{rename.error.message}</p>}<div className="form-actions"><button type="button" className="secondary-button" onClick={() => setRenameClient(null)}>Отмена</button><button className="primary-button" type="submit" disabled={rename.isPending}>{rename.isPending ? 'Сохранение…' : 'Сохранить'}</button></div></form></Dialog>}
    {qrClient && <Dialog title={`QR-код профиля ${qrClient.name}`} onClose={() => setQrClient(null)}><p>Отсканируйте код приложением WireGuard.</p><img className="qr-code" src={api.qrUrl(qrClient.id)} alt={`QR-код профиля ${qrClient.name}`} /></Dialog>}
    {credentialsDialogOpen && <Dialog title="Настроить доступ wg-easy" onClose={() => setCredentialsDialogOpen(false)}><p>Данные проверяются перед сохранением. Пароль не отображается и не сохраняется в браузере.</p><form onSubmit={submitCredentials}><label>Имя пользователя wg-easy<input name="username" required minLength={1} maxLength={255} autoComplete="username" autoFocus /></label><label>Пароль wg-easy<input name="password" type="password" required minLength={1} maxLength={1024} autoComplete="new-password" /></label>{credentialsError && <p className="form-error" role="alert">{credentialsError.message}</p>}<div className="form-actions"><button type="button" className="secondary-button" onClick={() => setCredentialsDialogOpen(false)}>Отмена</button><button className="primary-button" type="submit" disabled={credentialsSubmitting}>{credentialsSubmitting ? 'Проверка…' : 'Проверить и сохранить'}</button></div></form></Dialog>}
  </main>
}

function HandshakeTime({ value }: { value: string | null }) {
  if (!value) return <>нет данных</>
  return <><div>{formatDate(value)}</div><small>{formatElapsed(value)} назад</small></>
}

function ClientConnectionStatus({ client }: { client: Client }) {
  if (!client.enabled) return <StatusLabel state="pending" label="отключён" />
  if (!client.latest_handshake_at) return <StatusLabel state="danger" label="не подключался" />
  const handshakeAt = Date.parse(client.latest_handshake_at)
  const age = Date.now() - handshakeAt
  if (Number.isFinite(handshakeAt) && age >= -60_000 && age <= ACTIVE_HANDSHAKE_WINDOW_MS) {
    return <StatusLabel state="healthy" label="подключён" />
  }
  return <StatusLabel state="danger" label={`неактивен · ${formatElapsed(client.latest_handshake_at)}`} />
}

function StatusLabel({ state, label }: { state: 'healthy' | 'pending' | 'danger'; label: string }) {
  return <span className={`status-label ${state}`}><span className={`status-dot ${state}`} aria-hidden="true" />{label}</span>
}

function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  const id = `dialog-${title.replaceAll(' ', '-').toLowerCase()}`
  return <div className="dialog-backdrop"><section className="dialog" role="dialog" aria-modal="true" aria-labelledby={id}><button className="dialog-close" type="button" onClick={onClose} aria-label={title.startsWith('QR-код') ? 'Закрыть QR-код' : 'Закрыть'}>×</button><p className="eyebrow">WIREGUARD</p><h2 id={id}>{title}</h2>{children}</section></div>
}
