interface AsyncStateProps {
  loading: boolean
  error: Error | null
  children: React.ReactNode
  empty?: boolean
  emptyLabel?: string
}

export function AsyncState({ loading, error, children, empty = false, emptyLabel = 'Данных пока нет.' }: AsyncStateProps) {
  if (loading) return <p className="state-message" role="status">Загрузка данных…</p>
  if (error) return <p className="state-message error" role="alert">{error.message}</p>
  if (empty) return <p className="state-message">{emptyLabel}</p>
  return <>{children}</>
}
