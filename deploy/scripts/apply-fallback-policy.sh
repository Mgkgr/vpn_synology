#!/bin/sh
# Set the proven two-exit fallback cadence. This does not disable either exit
# and therefore is safe to run without a destructive failover exercise.
set -eu

PROJECT_DIR=${PROJECT_DIR:-/volume1/docker/vpn-gateway}
CONFIG=${CONFIG:-$PROJECT_DIR/mihomo/config.yaml}
DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
MIHOMO_CONTAINER=${MIHOMO_CONTAINER:-vpn-mihomo}
INTERVAL_SECONDS=${INTERVAL_SECONDS:-30}

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ "$INTERVAL_SECONDS" = 30 ] || { echo 'only the approved 30-second interval is accepted' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }
[ -f "$CONFIG" ] || { echo 'Mihomo config is missing' >&2; exit 1; }
"$DOCKER_BIN" inspect "$MIHOMO_CONTAINER" >/dev/null 2>&1 || { echo 'Mihomo container is missing' >&2; exit 1; }

# Restrict the edit to the one known group and refuse a configuration where
# this gateway is no longer the WG-IMP -> HY2-NL fallback design.
fallback_block=$(awk '
  /^  - name: VPS-FALLBACK$/ { capture = 1 }
  capture && /^  - name: / && $0 != "  - name: VPS-FALLBACK" { exit }
  capture { print }
' "$CONFIG")
printf '%s\n' "$fallback_block" | grep -q '^    type: fallback$' || { echo 'VPS-FALLBACK is not a fallback group' >&2; exit 1; }
printf '%s\n' "$fallback_block" | grep -q '^      - WG-IMP$' || { echo 'WG-IMP is missing from fallback' >&2; exit 1; }
printf '%s\n' "$fallback_block" | grep -q '^      - HY2-NL$' || { echo 'HY2-NL is missing from fallback' >&2; exit 1; }
[ "$(printf '%s\n' "$fallback_block" | grep -c '^    interval: ' || true)" = 1 ] || { echo 'VPS-FALLBACK must contain one interval line' >&2; exit 1; }

backup_dir="$PROJECT_DIR/backups/pre-fallback-policy-$(date -u +%Y%m%dT%H%M%SZ)"
umask 077
mkdir -p "$backup_dir"
cp -p "$CONFIG" "$backup_dir/config.yaml"
config_mode=$(stat -c '%a' "$CONFIG")
config_owner=$(stat -c '%u:%g' "$CONFIG")
config_tmp=$(mktemp "$CONFIG.fallback.XXXXXX")

restore() {
  cp -p "$backup_dir/config.yaml" "$CONFIG"
}
cleanup() {
  rm -f "$config_tmp"
}
trap cleanup EXIT INT TERM

awk -v interval="$INTERVAL_SECONDS" '
  /^  - name: VPS-FALLBACK$/ { in_fallback = 1 }
  in_fallback && /^  - name: / && $0 != "  - name: VPS-FALLBACK" { in_fallback = 0 }
  in_fallback && /^    interval: / { print "    interval: " interval; next }
  { print }
' "$CONFIG" > "$config_tmp"
chown "$config_owner" "$config_tmp"
chmod "$config_mode" "$config_tmp"
mv "$config_tmp" "$CONFIG"

if ! "$DOCKER_BIN" exec "$MIHOMO_CONTAINER" /mihomo -t -d /root/.config/mihomo; then
  restore
  echo 'RESULT=failed' >&2
  echo 'REASON=Mihomo configuration validation failed; backup was restored' >&2
  exit 1
fi
if ! "$DOCKER_BIN" restart "$MIHOMO_CONTAINER" >/dev/null; then
  restore
  "$DOCKER_BIN" restart "$MIHOMO_CONTAINER" >/dev/null || true
  echo 'RESULT=failed' >&2
  echo 'REASON=Mihomo restart failed; backup was restored' >&2
  exit 1
fi

echo 'RESULT=success'
echo "BACKUP_DIR=$backup_dir"
echo 'FALLBACK_GROUP=VPS-FALLBACK'
echo 'FALLBACK_ORDER=WG-IMP,HY2-NL'
echo "FALLBACK_INTERVAL_SECONDS=$INTERVAL_SECONDS"
echo 'FAILOVER_EXERCISE=not_run'
