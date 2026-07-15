#!/bin/sh
set -eu

# Run as root through deploy/fix-lan3-routing.ps1. It changes only the local
# 192.168.3.0/24 exception, not WireGuard clients, exits or fallback policy.
PROJECT_DIR=${PROJECT_DIR:-/volume1/docker/vpn-gateway}
CONFIG=${CONFIG:-$PROJECT_DIR/mihomo/config.yaml}
DIRECT_RULES=${DIRECT_RULES:-$PROJECT_DIR/mihomo/rules/direct.txt}
DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
MIHOMO_CONTAINER=${MIHOMO_CONTAINER:-vpn-mihomo}
LAN_CIDR=192.168.3.0/24
DIRECT_RULE="IP-CIDR,$LAN_CIDR,DIRECT,no-resolve"

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }
[ -f "$CONFIG" ] || { echo "Mihomo config is missing: $CONFIG" >&2; exit 1; }
[ -f "$DIRECT_RULES" ] || { echo "DIRECT rules are missing: $DIRECT_RULES" >&2; exit 1; }

config_entry="    - $LAN_CIDR"
backup_dir="$PROJECT_DIR/backups/pre-lan3-routing-$(date +%Y%m%dT%H%M%SZ)"
umask 077
mkdir -p "$backup_dir"
cp -p "$CONFIG" "$backup_dir/config.yaml"
cp -p "$DIRECT_RULES" "$backup_dir/direct.txt"

config_mode=$(stat -c '%a' "$CONFIG")
config_owner=$(stat -c '%u:%g' "$CONFIG")
rules_mode=$(stat -c '%a' "$DIRECT_RULES")
rules_owner=$(stat -c '%u:%g' "$DIRECT_RULES")
config_tmp=$(mktemp "$CONFIG.lan3.XXXXXX")
rules_tmp=$(mktemp "$DIRECT_RULES.lan3.XXXXXX")

restore() {
  cp -p "$backup_dir/config.yaml" "$CONFIG"
  cp -p "$backup_dir/direct.txt" "$DIRECT_RULES"
}

cleanup() {
  rm -f "$config_tmp" "$rules_tmp"
}
trap cleanup EXIT

if grep -Fqx "$config_entry" "$CONFIG"; then
  cp "$CONFIG" "$config_tmp"
else
  anchors=$(grep -c '^    - 192\.168\.2\.0/24$' "$CONFIG" || true)
  [ "$anchors" = 1 ] || { echo 'expected 192.168.2.0/24 TUN exclusion was not found exactly once' >&2; exit 1; }
  sed '/^    - 192\.168\.2\.0\/24$/a\
    - 192.168.3.0/24' "$CONFIG" > "$config_tmp"
fi

if grep -Fqx "$DIRECT_RULE" "$DIRECT_RULES"; then
  cp "$DIRECT_RULES" "$rules_tmp"
else
  cp "$DIRECT_RULES" "$rules_tmp"
  printf '%s\n' "$DIRECT_RULE" >> "$rules_tmp"
fi

chown "$config_owner" "$config_tmp"
chmod "$config_mode" "$config_tmp"
chown "$rules_owner" "$rules_tmp"
chmod "$rules_mode" "$rules_tmp"
mv "$config_tmp" "$CONFIG"
mv "$rules_tmp" "$DIRECT_RULES"

if ! "$DOCKER_BIN" exec "$MIHOMO_CONTAINER" mihomo -t -d /root/.config/mihomo; then
  restore
  echo 'RESULT=failed' >&2
  echo 'REASON=Mihomo configuration validation failed; backups were restored' >&2
  exit 1
fi

if ! "$DOCKER_BIN" restart "$MIHOMO_CONTAINER" >/dev/null; then
  restore
  "$DOCKER_BIN" restart "$MIHOMO_CONTAINER" >/dev/null || true
  echo 'RESULT=failed' >&2
  echo 'REASON=Mihomo restart failed; backups were restored' >&2
  exit 1
fi

echo 'RESULT=success'
echo "BACKUP_DIR=$backup_dir"
echo "LAN_DIRECT_CIDR=$LAN_CIDR"
echo "MIHOMO_CONTAINER=$MIHOMO_CONTAINER"
