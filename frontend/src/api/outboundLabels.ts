// Stable IDs keep policies and historical records linked when providers change.
// These labels describe the current endpoints, not their historical countries.
export function outboundLabel(value: string | null | undefined, empty = '—'): string {
  if (value === 'WG-IMP') return 'VLESS-NL'
  if (value === 'HY2-USA') return 'HY2-DE'
  return value || empty
}

export function outboundId(value: string): string {
  const normalized = value.trim()
  const alias = normalized.toUpperCase()
  if (alias === 'VLESS-NL' || alias === 'VLESS-USA') return 'WG-IMP'
  if (alias === 'HY2-DE' || alias === 'HY2-USA') return 'HY2-USA'
  return normalized
}
