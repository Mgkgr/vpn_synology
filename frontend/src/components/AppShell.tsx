import type { PropsWithChildren } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'

import { api } from '../api/client'

const links = [
  ['/overview', 'Обзор', '01'],
  ['/routes', 'Маршруты', '02'],
  ['/clients', 'Клиенты', '03'],
  ['/rules', 'Правила', '04'],
  ['/updates', 'Обновления', '05'],
  ['/journal', 'Журнал', '06'],
] as const

export function AppShell({ children }: PropsWithChildren) {
  const navigate = useNavigate()

  async function handleLogout() {
    try {
      await api.logout()
    } finally {
      api.clearSession()
      navigate('/login', { replace: true })
    }
  }

  return (
    <div className="app-shell">
      <aside className="rail" aria-label="Основная навигация">
        <NavLink to="/overview" className="wordmark" aria-label="VPN центр управления">
          <span className="wordmark-mark">V</span><span>VPN</span>
          <small>ЦЕНТР УПРАВЛЕНИЯ</small>
        </NavLink>
        <nav>
          <ul>
            {links.map(([to, label, index]) => (
              <li key={to}>
                <NavLink to={to} className={({ isActive }) => isActive ? 'nav-link is-active' : 'nav-link'}>
                  <span aria-hidden="true">{index}</span>{label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
        <div className="rail-footer">
          <span className="status-dot healthy" aria-hidden="true" /> LAN / WireGuard
          <button className="text-button" type="button" onClick={handleLogout}>Выйти</button>
        </div>
      </aside>
      <section className="workspace">{children}</section>
    </div>
  )
}
