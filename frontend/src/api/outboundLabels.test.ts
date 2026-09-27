import { describe, expect, it } from 'vitest'

import { outboundId, outboundLabel } from './outboundLabels'

describe('outbound display aliases', () => {
  it('shows the current countries for stable primary and reserve IDs', () => {
    expect(outboundLabel('WG-IMP')).toBe('VLESS-NL')
    expect(outboundLabel('HY2-USA')).toBe('HY2-DE')
    expect(outboundLabel('DIRECT')).toBe('DIRECT')
    expect(outboundLabel(null)).toBe('—')
  })

  it('maps current and old labels back to IDs accepted by rule and journal APIs', () => {
    expect(outboundId(' vless-nl ')).toBe('WG-IMP')
    expect(outboundId('VLESS-USA')).toBe('WG-IMP')
    expect(outboundId('hy2-de')).toBe('HY2-USA')
    expect(outboundId('HY2-USA')).toBe('HY2-USA')
    expect(outboundId('DIRECT')).toBe('DIRECT')
    expect(outboundId('')).toBe('')
  })
})
