import type { ComponentId, MaintenanceJob } from '../api/maintenanceTypes'
import { ProfileProbeResults } from './ProfileProbeResults'

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
  strategy_verification_failed: 'Проверка после применения не прошла. Предыдущая конфигурация возвращена; её доступность этим не подтверждается.',
  strategy_infrastructure_unavailable: 'Условия безопасной проверки Anti-DPI не подтверждены. Стратегия не считается неисправной.',
  strategy_dns_changed: 'DNS изменился во время сравнения. Применение отменено; нужна новая проверка.',
  strategy_candidate_failed: 'Кандидат не прошёл три успешные проверки. Текущая стратегия сохранена.',
  strategy_no_candidate: 'Нет подходящей предыдущей или новой стратегии.',
  strategy_backup_unavailable: 'Зашифрованная копия не подтверждена. Применение не начато.',
  strategy_deadline: 'Истёк лимит времени задания.',
  strategy_attempt_limit: 'Достигнут предел числа попыток.',
  strategy_policy_revoked: 'Разрешение автоматической проверки отозвано.',
  profile_check_required: 'Нужна успешная проверка этого ключа не старше пяти минут.',
  profile_check_failed: 'Новый ключ не прошёл повторные проверки. Рабочий маршрут не изменён.',
  profile_backup_failed: 'Резервная копия не подтверждена. Применение не начато.',
  profile_reverted: 'Проверка после замены не прошла; прежняя конфигурация возвращена. Её доступность этим не подтверждается.',
  profile_unavailable: 'Проверка не завершилась: проверьте доступность исполнителя, DNS сервера и конфигурацию на NAS.',
  probe_cleanup_unconfirmed: 'Не удалось подтвердить удаление тестового контейнера. Замены заблокированы до проверки на NAS; рабочий ключ не изменён.',
  profile_deadline: 'Истёк лимит времени проверки ключа.',
  draft_unavailable: 'Черновик истёк или недоступен. Вставьте ссылку заново.',
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
      {job.strategy && <p role="status">Попыток завершено: {job.strategy.completed} · предел {job.strategy.limit}. {({ queued: 'Ожидает запуска', checking: 'Проверяются стратегии', backup: 'Создаётся копия', applying: 'Применяется кандидат', verifying: 'Проверяется реальный вход Anti-DPI', verified: 'Проверка после применения успешна', rollback: 'Возвращается конфигурация', finished: 'Проверки закончены' } as Record<string, string>)[job.strategy.step] ?? 'Проверка условий'}</p>}
      {job.profile && <><p role="status">{job.profile.action === 'profile_check' ? 'Новый ключ · изолированная проверка' : 'Применение ключа'} · завершено проб {job.profile.completed} из {job.profile.limit}</p><ProfileProbeResults results={job.profile.results} /></>}
      {['unknown', 'needs_reconcile'].includes(job.phase) && <p>Не запускайте повторно. Страница проверяет прежний ID задания; закрытие вкладки не отменяет операцию.</p>}
      {job.maintenance_until && !FINISHED_PHASES.has(job.phase) && <p>Плановое окно до {maintenanceDate(job.maintenance_until)}. Это не подтверждение исправности VPN; окно автоматически не продлевается.</p>}
      {job.cancel_requested && <p>Отмена запрошена; ожидается безопасное завершение текущего этапа.</p>}
      {onCancel && job.cancel_allowed && <button type="button" className="secondary-button" onClick={onCancel}>Отменить задание</button>}
    </div>
  </section>
}
