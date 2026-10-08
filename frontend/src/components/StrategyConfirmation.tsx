import { useEffect, useRef, useState } from 'react'
import type { StrategyOperation } from '../api/antidpiTypes'
import type { CancelOperation } from '../api/maintenanceTypes'

type Operation = StrategyOperation | CancelOperation
const LABELS = { strategy_check: 'Проверить текущую стратегию', strategy_tune: 'Подобрать стратегии',
  strategy_configure: 'Сохранить режим и расписание', strategy_apply: 'Применить стратегию',
  strategy_rollback: 'Вернуть предыдущую стратегию', cancel: 'Отменить задание' }

export function StrategyConfirmation({ operation, serviceName, onConfirm, onClose }: {
  operation: Operation; serviceName: string; onConfirm: (password: string) => Promise<void>; onClose: () => void
}) {
  const dialog = useRef<HTMLElement>(null), passwordInput = useRef<HTMLInputElement>(null), submitting = useRef(false)
  const [password, setPassword] = useState(''), [pending, setPending] = useState(false), [error, setError] = useState<string | null>(null)
  useEffect(() => { const previous = document.activeElement as HTMLElement | null; passwordInput.current?.focus(); return () => previous?.focus() }, [])
  function keyDown(event: React.KeyboardEvent) {
    if (event.key === 'Escape' && !submitting.current) { event.preventDefault(); onClose() }
    if (event.key === 'Tab') {
      const items = [...(dialog.current?.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled])') ?? [])]
      const first = items[0], last = items.at(-1)
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (submitting.current || !password) return
    submitting.current = true; setPending(true); setError(null)
    try { await onConfirm(password) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Подтверждение не выполнено.') }
    finally { setPassword(''); setPending(false); submitting.current = false }
  }
  const changes = ['strategy_apply', 'strategy_rollback'].includes(operation.action)
  return <div className="dialog-backdrop"><section className="dialog maintenance-confirm" role="dialog" aria-modal="true" aria-labelledby="strategy-confirm-title" ref={dialog} onKeyDown={keyDown}>
    <h2 id="strategy-confirm-title">{LABELS[operation.action]}</h2><p>{serviceName}</p>
    {operation.action === 'strategy_apply' && <p>После применения: {operation.settings.mode === 'auto' ? 'Авто' : 'Закрепить'} · {operation.settings.strategy_id}</p>}
    {changes ? <p>Кандидат будет проверен до применения. Возможен краткий разрыв соединений Anti-DPI; VLESS, Hysteria2 и WireGuard не перезапускаются.</p>
      : <p>Ручная проверка и подбор не меняют стратегию. Автосмена разрешается только отдельной настройкой режима «Авто».</p>}
    {operation.action === 'strategy_configure' && operation.settings.enabled && operation.settings.mode === 'auto' && <p>Разрешается автосмена только этого сервиса в текущем каталоге: после трёх отказов, не чаще раза в 15 минут и двух раз в час. При смене версии разрешение отзывается.</p>}
    <form onSubmit={submit}><label>Пароль владельца<input ref={passwordInput} type="password" autoComplete="current-password" maxLength={1024} value={password} disabled={pending} onChange={event => setPassword(event.target.value)} required /></label>
      {error && <p role="alert" className="form-error">{error}</p>}
      <div className="form-actions"><button type="button" className="secondary-button" disabled={pending} onClick={onClose}>Отмена</button><button className="primary-button" type="submit" disabled={pending || !password}>{pending ? 'Подтверждение…' : 'Подтвердить'}</button></div>
    </form>
  </section></div>
}
