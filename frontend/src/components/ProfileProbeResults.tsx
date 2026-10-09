import type { ProfileProbe } from '../api/profileTypes'

const TARGETS = { cloudflare: 'Cloudflare', google: 'Google', github: 'GitHub' }
const REASONS: Record<string, string> = { timeout: 'Тайм-аут', tls_failed: 'Ошибка TLS', connect_failed: 'Соединение не установлено', http_status: 'Неожиданный HTTP-ответ', invalid_response: 'Неполный ответ', probe_failed: 'Проверка не подтверждена' }

export function ProfileProbeResults({ results }: { results: ProfileProbe[] }) {
  if (!results.length) return null
  return <div className="profile-results" aria-label="Результаты проверки ключа"><table><thead><tr><th>Сайт · попытка</th><th>Время</th><th>Результат</th></tr></thead>
    <tbody>{results.map((row, index) => <tr key={`${row.target}-${index}`}><td>{TARGETS[row.target]} · {Math.floor(index / 3) + 1}</td>
      <td>{new Date(row.checked_at * 1000).toLocaleTimeString('ru-RU')}</td>
      <td>{row.ok ? `${row.latency_ms} мс · HTTP ${row.http_status}` : `${REASONS[row.reason ?? 'probe_failed'] ?? 'Ошибка'}${row.http_status ? ` · HTTP ${row.http_status}` : ''}`}</td></tr>)}</tbody></table></div>
}
