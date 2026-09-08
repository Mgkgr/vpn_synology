export function Status({ ok, pending = false }: { ok: boolean | null | undefined; pending?: boolean }) {
  const state = pending ? 'pending' : ok ? 'healthy' : 'danger'
  const label = pending ? 'ожидает' : ok ? 'в норме' : 'недоступен'
  return <span className={`status-label ${state}`}><span className={`status-dot ${state}`} aria-hidden="true" />{label}</span>
}

export function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '0 Б'
  const units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ']
  const exponent = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1)
  return `${(value / 1024 ** exponent).toFixed(exponent === 0 ? 0 : 1)} ${units[exponent]}`
}

export function formatRate(value: number): string {
  return `${formatBytes(value)}/с`
}

export function formatDate(value: string | null): string {
  if (!value) return 'нет данных'
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat('ru-RU', { dateStyle: 'short', timeStyle: 'short' }).format(date)
}

export function formatElapsed(value: string | null | undefined, now = Date.now()): string {
  if (!value) return 'нет данных'
  const observedAt = Date.parse(value)
  if (!Number.isFinite(observedAt)) return 'нет данных'
  const seconds = Math.max(0, Math.floor((now - observedAt) / 1_000))
  if (seconds < 60) return 'меньше минуты'
  if (seconds < 3_600) return `${Math.floor(seconds / 60)} мин`
  if (seconds < 86_400) return `${Math.floor(seconds / 3_600)} ч`
  return `${Math.floor(seconds / 86_400)} д`
}
