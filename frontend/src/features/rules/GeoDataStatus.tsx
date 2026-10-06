import type { GeoUpdate, UpdatesResponse } from '../../api/types'
import { formatBytes, formatDate, formatElapsed } from '../../components/Status'

export function geoUpdateSummary(update: GeoUpdate | undefined): string {
  if (!update) return 'Запрос принят. Завершение обновления ещё не подтверждено.'
  if (update.verification === 'pending') return 'Обновление выполняется. Ожидаем результат Mihomo.'
  if (update.verification === 'changed') return `Содержимое изменилось: ${update.changed_files.join(', ')}. HTTP ${update.status_code ?? '—'}.`
  if (update.verification === 'unchanged') return `Содержимое файлов не изменилось: ${update.checked_files.join(', ') || 'GeoData'}. HTTP ${update.status_code ?? '—'}.`
  if (update.verification === 'unavailable') return `Mihomo ответил HTTP ${update.status_code ?? '—'}, но результат по файлам не подтверждён.`
  if (update.verification === 'snapshot') return 'Снимок файлов, не обновление из источника.'
  return `Обновление не подтверждено${update.status_code ? ` · HTTP ${update.status_code}` : ''}. Состояние файлов проверяется отдельно.`
}
function Time({ value }: { value: string | null | undefined }) {
  return value ? <time dateTime={value}>{formatDate(value)} · {formatElapsed(value)} назад</time> : <>нет данных</>
}
export function GeoDataStatus({ data, loading, error }: { data?: UpdatesResponse; loading: boolean; error: Error | null }) {
  const lastAttempt = data?.updates.find((update) => update.operation === 'geo_upgrade')
  return <section className="panel geodata-status-panel" aria-labelledby="geo-status-title">
    <div className="section-heading"><h2 id="geo-status-title">GeoSite / GeoIP — состояние данных</h2><span>Автообновление: ежедневно в 04:00 · Asia/Yekaterinburg</span></div>
    {loading && <p role="status" className="rules-workspace-body">Читаем состояние GeoData…</p>}
    {error && <p role="alert" className="form-error rules-workspace-body">Состояние GeoData получить не удалось. Правила доступны независимо.</p>}
    <div className="geo-status-grid">{['GeoSite', 'GeoIP'].map((kind) => {
      const asset = data?.assets?.find((item) => item.kind === kind)
      const lastSuccess = data?.updates.find((update) => update.operation === 'geo_upgrade' && update.succeeded === true && ['changed', 'unchanged'].includes(update.verification) && update.checked_files.some((name) => kind === 'GeoSite' ? /site/i.test(name) : /ip|mmdb/i.test(name)))
      return <section className="geo-status-card" role="region" aria-label={`Состояние ${kind}`} key={kind}>
        <h3>{kind}</h3><p className={asset && !error ? 'success-message' : 'rules-hint'}>{asset ? error ? 'Снимок устарел; доступность не подтверждена' : 'Файл доступен' : loading ? 'Проверяется…' : 'Файл не подтверждён'}</p>
        <dl><dt>Дата файла</dt><dd><Time value={asset?.modified_at} /></dd><dt>Последнее наблюдение в журнале</dt><dd><Time value={asset?.last_observed_at} /></dd><dt>Последнее успешное обновление</dt><dd><Time value={lastSuccess?.observed_at} /></dd></dl>
        {asset && <p className="geo-version">{asset.filename} · {formatBytes(asset.size_bytes)}<br />SHA-256: <code title={asset.sha256}>{asset.sha256.slice(0, 12)}…</code></p>}
        {data?.assets_error && <p className="form-error">Не все данные файла доступны для проверки.</p>}
        <details><summary>Источник и применение</summary><p>Обновлением управляет Mihomo. Источники отдельно не проверены: доступность файла не доказывает доступность сервера обновлений.</p><p>Версия в памяти Mihomo не подтверждена. Хеш относится к установленному файлу.</p><p>Категории {kind} используют один файл; отдельных дат обновления категорий нет.</p></details>
      </section>
    })}</div>
    <div className="geo-last-result"><strong>Последняя попытка: </strong><Time value={lastAttempt?.observed_at} /><p>{lastAttempt ? geoUpdateSummary(lastAttempt) : 'Попытки обновления ещё не зарегистрированы.'}</p></div>
  </section>
}
