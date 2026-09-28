import { useEffect, useRef, useState } from 'react'
import type { CancelOperation, ComponentId, MaintenanceOperation } from '../api/maintenanceTypes'
import { COMPONENT_NAMES } from './MaintenanceJobPanel'

type Operation = MaintenanceOperation | CancelOperation
export function MaintenanceConfirmation({ operation, affected, stopped, onConfirm, onClose }: {
  operation: Operation; affected: ComponentId[]; stopped: ComponentId[]
  onConfirm: (operation: Operation, password: string) => Promise<void>; onClose: () => void
}) {
  const dialog = useRef<HTMLElement>(null)
  const passwordInput = useRef<HTMLInputElement>(null)
  const submitting = useRef(false)
  const [password, setPassword] = useState('')
  const [enabled, setEnabled] = useState<ComponentId[]>([])
  const [loss, setLoss] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    passwordInput.current?.focus()
    return () => previous?.focus()
  }, [])
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
    if (submitting.current || !password || stopped.some(id => !enabled.includes(id)) || (operation.action === 'rollback' && !loss)) return
    submitting.current = true; setPending(true); setError(null)
    try {
      const confirmed = operation.action === 'cancel' ? operation : { ...operation, enable_stopped: enabled, accept_data_loss: operation.action === 'rollback' && loss }
      await onConfirm(confirmed, password)
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Подтверждение не выполнено.') }
    finally { setPassword(''); setPending(false); submitting.current = false }
  }
  return <div className="dialog-backdrop"><section className="dialog maintenance-confirm" ref={dialog} role="dialog" aria-modal="true" aria-labelledby="maintenance-title" onKeyDown={keyDown}>
    <h2 id="maintenance-title">Подтвердить обслуживание</h2>
    <p>Будут затронуты: {affected.map(id => COMPONENT_NAMES[id]).join(', ') || 'текущее задание'}.</p>
    <p>VPN и панель могут кратко отключиться. NAS и посторонние проекты не перезапускаются.</p>
    {operation.action === 'rollback' && <p className="form-error">Восстановление выбранной копии может удалить клиентов и изменения, появившиеся после её создания.</p>}
    <form onSubmit={submit}>
      {stopped.map(id => <label className="maintenance-check" key={id}><input type="checkbox" checked={enabled.includes(id)} disabled={pending} onChange={event => setEnabled(items => event.target.checked ? [...items, id] : items.filter(item => item !== id))} />Разрешить запуск остановленного {COMPONENT_NAMES[id]}</label>)}
      {operation.action === 'rollback' && <label className="maintenance-check"><input type="checkbox" checked={loss} disabled={pending} onChange={event => setLoss(event.target.checked)} />Подтверждаю потерю более новых данных в выбранных компонентах</label>}
      <label>Пароль владельца<input ref={passwordInput} type="password" autoComplete="current-password" maxLength={1024} value={password} disabled={pending} onChange={event => setPassword(event.target.value)} required /></label>
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="form-actions"><button type="button" className="secondary-button" onClick={onClose} disabled={pending}>Отмена</button><button type="submit" className="primary-button" disabled={pending || !password || stopped.some(id => !enabled.includes(id)) || (operation.action === 'rollback' && !loss)}>{pending ? 'Подтверждение…' : 'Подтвердить'}</button></div>
    </form>
  </section></div>
}
