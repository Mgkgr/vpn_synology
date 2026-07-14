import { describe, expect, it } from 'vitest'

import { applyBuildStamp, BUILD_ID } from './build'

describe('build stamp', () => {
  it('marks the document with the current build identifier', () => {
    document.documentElement.removeAttribute('data-vpn-dashboard-build')

    applyBuildStamp(document)

    expect(document.documentElement.dataset.vpnDashboardBuild).toBe(BUILD_ID)
  })
})
