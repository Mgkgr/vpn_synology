import type { ComponentId, MaintenanceJob } from '../api/maintenanceTypes'

export const COMPONENT_NAMES: Record<ComponentId, string> = {
  wireguard: 'WireGuard / wg-easy', mihomo: 'Mihomo', 'uptime-kuma': 'Uptime Kuma',
  metacubexd: 'MetaCubeXD', dashboard: 'Панель VPN', antidpi: 'AntiDPI',
}
export const FINISHED_PHASES = new Set(['completed', 'failed', 'cancelled', 'not_started'])
const PHASES: Record<string, string> = {
  queued: 'В очереди', preflight: 'Проверка условий', backup: 'Зашифрованная резервная копия',
  download: 'Загрузка и проверка версии', apply: 'Применение', verify: 'Проверка работоспособности',
  rollback: 'Возврат предыдущего состояния', completed: 'Завершено', failed: 'Ошибка',
  cancelled: 'Отменено', needs_reconcile: 'Требуется проверка состояния на NAS', unknown: 'Результат пока не подтверждён',
  not_started: 'Не запущено',
}
const REASONS: Record<string, string> = {
  revision_changed: 'Настройки изменились после подтверждения. Нужно заново проверить состав операции.',
  update_reverted: 'Обновление не прошло проверку; предыдущая версия восстановлена и проверена.',
  restart_failed: 'Перезапуск не подтверждён. Автоматически повторять его нельзя.',
  rollback_failed: 'Возврат не подтверждён. Требуется проверка владельцем на NAS.',
  worker_interrupted: 'Исполнитель был прерван; результат не считается успешным.',
  insufficient_resources: 'Недостаточно подтверждённых ресурсов для безопасной операции.',
  schema_migration_unverified: 'Совместимость базы данных и путь отката не подтверждены.',
  release_unverified: 'Эта версия ещё не разрешена для установки.',
}
export function maintenanceDate(value: number | null | undefined) {
  return value ? new Date(value * 1000).toLocaleString('ru-RU') : 'Нет данных'
}
export function MaintenanceJobPanel({ job, onCancel }: { job: MaintenanceJob; onCancel?: () => void }) {
  return <section className="panel maintenance-job" aria-label="Ход обслуживания">
    <div className="section-heading"><h2>Ход обслуживания</h2><span role="status">{PHASES[job.phase] ?? PHASES.unknown}</span></div>
    <div className="maintenance-body"><code className="job-id">{job.job_id}</code>
      <dl><dt>Компонент</dt><dd>{job.component ? COMPONENT_NAMES[job.component] : 'Комплекс / уточняется'}</dd>
        <dt>Администратор</dt><dd>{job.actor ?? 'Уточняется'}</dd>
        <dt>Начало</dt><dd>{maintenanceDate(job.started_at)}</dd><dt>Завершение</dt><dd>{maintenanceDate(job.finished_at)}</dd></dl>
      {job.error_code && <p role="alert">{REASONS[job.error_code] ?? 'Операция не завершена. Нужна проверка состояния; повторный запуск автоматически не выполняется.'}</p>}
      {['unknown', 'needs_reconcile'].includes(job.phase) && <p>Не запускайте повторно. Страница проверяет прежний ID задания; закрытие вкладки не отменяет операцию.</p>}
      {job.maintenance_until && !FINISHED_PHASES.has(job.phase) && <p>Плановое окно до {maintenanceDate(job.maintenance_until)}. Это не подтверждение исправности VPN; окно автоматически не продлевается.</p>}
      {job.cancel_requested && <p>Отмена запрошена; ожидается безопасное завершение текущего этапа.</p>}
      {onCancel && job.cancel_allowed && <button type="button" className="secondary-button" onClick={onCancel}>Отменить задание</button>}
    </div>
  </section>
}
