#!/bin/sh
# Enable Mihomo's documented pure-IP domain sniffing without changing routes,
# DNS resolvers, exits, WireGuard or the dashboard. The previous configuration
# is both copied for rollback and archived with the dashboard encryption key.
set -eu

CONFIGURATOR=${1:?missing configurator path}
GATEWAY_DIR=${GATEWAY_DIR:-/volume1/docker/vpn-gateway}
CONFIG=${CONFIG:-$GATEWAY_DIR/mihomo/config.yaml}
BACKUP_DIR=${BACKUP_DIR:-$GATEWAY_DIR/backups}
SECRET_FILE=${DASHBOARD_SECRET_FILE:-/volume1/docker/vpn-dashboard/deploy/secrets/dashboard_encryption_key}
DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
MIHOMO_CONTAINER=${MIHOMO_CONTAINER:-vpn-mihomo}

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -f "$CONFIGURATOR" ] || { echo 'sniffer configurator is missing' >&2; exit 1; }
[ -f "$CONFIG" ] || { echo 'Mihomo config is missing' >&2; exit 1; }
[ -f "$SECRET_FILE" ] || { echo 'dashboard backup secret is missing' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo 'openssl is unavailable' >&2; exit 1; }
[ -x /usr/bin/python3 ] || { echo 'python3 is unavailable' >&2; exit 1; }
"$DOCKER_BIN" inspect "$MIHOMO_CONTAINER" >/dev/null 2>&1 || { echo 'Mihomo container is missing' >&2; exit 1; }

umask 077
mkdir -p "$BACKUP_DIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
archive="$BACKUP_DIR/mihomo-before-domain-sniffer-$stamp.tar.enc"
tar -C "$GATEWAY_DIR" -cf - mihomo/config.yaml | openssl enc -aes-256-cbc -pbkdf2 -salt -pass "file:$SECRET_FILE" -out "$archive"
chmod 0600 "$archive"

previous=$(mktemp "$GATEWAY_DIR/mihomo/.config.sniffer.previous.XXXXXX")
cp -p "$CONFIG" "$previous"
cleanup() { rm -f "$previous"; }
trap cleanup EXIT INT TERM

if /usr/bin/python3 "$CONFIGURATOR" "$CONFIG"; then
  :
else
  status=$?
  if [ "$status" -eq 3 ]; then
    echo 'RESULT=not_applied'
    echo 'REASON=sniffer is already configured; refusing to overwrite it'
    exit 3
  fi
  echo 'RESULT=failed' >&2
  echo 'REASON=sniffer configuration could not be written' >&2
  exit "$status"
fi

restore() {
  cp -p "$previous" "$CONFIG"
  "$DOCKER_BIN" restart "$MIHOMO_CONTAINER" >/dev/null || true
}

if ! "$DOCKER_BIN" exec "$MIHOMO_CONTAINER" /mihomo -t -d /root/.config/mihomo; then
  restore
  echo 'RESULT=failed' >&2
  echo 'REASON=Mihomo configuration validation failed; backup was restored' >&2
  exit 1
fi
if ! "$DOCKER_BIN" restart "$MIHOMO_CONTAINER" >/dev/null; then
  restore
  echo 'RESULT=failed' >&2
  echo 'REASON=Mihomo restart failed; backup was restored' >&2
  exit 1
fi

echo 'RESULT=success'
echo 'SNIFFER=enabled'
echo 'PURE_IP_SNIFFING=enabled'
echo "ENCRYPTED_BACKUP=$(basename "$archive")"
