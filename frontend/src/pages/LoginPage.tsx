import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api } from '../api/client'

export function LoginPage({ onAuthenticated }: { onAuthenticated: () => void }) {
  const navigate = useNavigate()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const username = String(form.get('username') ?? '').trim()
    const password = String(form.get('password') ?? '')
    setPending(true)
    setError(null)
    try {
      await api.login(username, password)
      onAuthenticated()
      navigate('/overview', { replace: true })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Не удалось выполнить вход.')
    } finally {
      setPending(false)
    }
  }

  return <main className="login-page"><section className="login-panel"><div className="wordmark"><span className="wordmark-mark">V</span><span>VPN</span><small>ЦЕНТР УПРАВЛЕНИЯ</small></div><p className="eyebrow">ЗАЩИЩЁННАЯ ПАНЕЛЬ</p><h1>Вход администратора</h1><p>Сессия хранится в защищённой HTTP-only cookie.</p><form onSubmit={submit}><label>Пользователь<input name="username" required autoComplete="username" /></label><label>Пароль<input name="password" type="password" required minLength={1} autoComplete="current-password" /></label>{error && <p className="form-error" role="alert">{error}</p>}<button className="primary-button" type="submit" disabled={pending}>{pending ? 'Проверка…' : 'Войти'}</button></form></section></main>
}
