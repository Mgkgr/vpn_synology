// Change this identifier for every browser-visible dashboard release. It is
// intentionally included in the entry bundle so Vite emits a new asset URL.
export const BUILD_ID = '2026-07-14-wgeasy-qr-1'

export function applyBuildStamp(documentRef: Document): void {
  documentRef.documentElement.dataset.vpnDashboardBuild = BUILD_ID
}
