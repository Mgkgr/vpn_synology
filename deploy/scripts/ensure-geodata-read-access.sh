#!/bin/sh
# Give the unprivileged dashboard process read-only access to GeoData.
# The inherited directory ACE keeps the grant when Mihomo replaces a file.
set -eu

MIHOMO_DIR=${MIHOMO_DIR:-/volume1/docker/vpn-gateway/mihomo}
DASHBOARD_UID=${DASHBOARD_UID:-10001}
SYNOACL_BIN=${SYNOACL_BIN:-/usr/syno/bin/synoacltool}

case "$DASHBOARD_UID" in ''|*[!0-9]*) echo 'DASHBOARD_UID must be numeric' >&2; exit 1 ;; esac
[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -d "$MIHOMO_DIR" ] || { echo "Mihomo directory is missing: $MIHOMO_DIR" >&2; exit 1; }
[ -x "$SYNOACL_BIN" ] || { echo "synoacltool is unavailable: $SYNOACL_BIN" >&2; exit 1; }

ensure_ace() {
  path=$1
  ace=$2
  if ! "$SYNOACL_BIN" -getace "$path" 2>/dev/null | grep -Fq "$ace"; then
    "$SYNOACL_BIN" -addace "$path" "$ace"
  fi
}

# The parent ACE is inherited by files that Mihomo downloads into this directory.
ensure_ace "$MIHOMO_DIR" "user:$DASHBOARD_UID:allow:r-x---a-R-c--:fd--"
for filename in GeoIP.dat GeoSite.dat; do
  path="$MIHOMO_DIR/$filename"
  [ -f "$path" ] || { echo "GeoData file is missing: $path" >&2; exit 1; }
  ensure_ace "$path" "user:$DASHBOARD_UID:allow:r-----a-R-c--:----"
done

echo 'GEODATA_ACL=ready'
