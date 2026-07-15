import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import { AsyncState } from '../components/AsyncState'

export function AdministratorsPage() {
  const queryClient = useQueryClient()
  const [notice, setNotice] = useState<string | null>(null)
  const administrators = useQuery({ queryKey: ['administrators'], queryFn: api.administrators })
  const create = useMutation({ mutationFn: ({ username, password }: { username: string; password: string }) => api.createAdministrator(username, password), onSuccess: async () => { setNotice('Администратор создан.'); await queryClient.invalidateQueries({ queryKey: ['administrators'] }) } })
  const revoke = useMutation({ mutationFn: api.revokeAdministratorSessions, onSuccess: (_data, username) => setNotice(`Все сессии «${username}» отозваны.`) })
  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const username = String(form.get('username') ?? '').trim()
    const password = String(form.get('password') ?? '')
    if (username && password) create.mutate({ username, password })
  }
  return <main className="page"><header className="page-header"><div><p className="eyebrow">07 / ДОСТУП</p><h1>Администраторы</h1><p>Управляйте локальными учётными записями и немедленно отзывайте сессии при необходимости.</p></div></header>
    <section className="panel"><div className="section-heading"><h2>Учётные записи</h2></div><AsyncState loading={administrators.isLoading} error={administrators.error} empty={!administrators.data?.length} emptyLabel="Администраторов пока нет."><table className="data-table"><thead><tr><th>Имя</th><th>Роль</th><th>Действие</th></tr></thead><tbody>{administrators.data?.map((admin) => <tr key={admin.username}><th scope="row">{admin.username}</th><td>{admin.bootstrap_owner ? 'Владелец' : 'Администратор'}</td><td><button className="text-button danger" type="button" disabled={revoke.isPending} aria-label={`Отозвать сессии ${admin.username}`} onClick={() => revoke.mutate(admin.username)}>Отозвать сессии</button></td></tr>)}</tbody></table></AsyncState>{notice && <p role="status">{notice}</p>}</section>
    <section className="panel"><div className="section-heading"><h2>Новый администратор</h2></div><form className="inline-form" onSubmit={submit}><label>Имя<input name="username" required minLength={1} maxLength={255} autoComplete="username" /></label><label>Пароль<input name="password" type="password" required minLength={12} maxLength={1024} autoComplete="new-password" /></label><button className="primary-button" disabled={create.isPending} type="submit">{create.isPending ? 'Создание…' : 'Создать'}</button></form>{create.error && <p className="form-error" role="alert">{create.error.message}</p>}</section>
  </main>
}
