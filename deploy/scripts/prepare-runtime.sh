#!/bin/sh
set -eu

# Run as root on the NAS immediately before vpn-dashboard-deploy.
# This script never touches WireGuard, Mihomo or their running containers.
PROJECT_DIR=${PROJECT_DIR:-/volume1/docker/vpn-dashboard}
MIHOMO_RULES_DIR=${MIHOMO_RULES_DIR:-/volume1/docker/vpn-gateway/mihomo/rules}
APP_UID=${APP_UID:-10001}
APP_GID=${APP_GID:-10001}

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -f "$PROJECT_DIR/deploy/dashboard.env" ] || { echo 'dashboard.env is missing' >&2; exit 1; }
grep -q '^DASHBOARD_ENCRYPTION_KEY=.' "$PROJECT_DIR/deploy/dashboard.env" || { echo 'DASHBOARD_ENCRYPTION_KEY is missing' >&2; exit 1; }
# Preserve all secrets and integration URLs; only remove the direct LAN bind.
if grep -q '^DASHBOARD_BIND=' "$PROJECT_DIR/deploy/dashboard.env"; then
  sed -i 's|^DASHBOARD_BIND=.*$|DASHBOARD_BIND=127.0.0.1:8088|' "$PROJECT_DIR/deploy/dashboard.env"
else
  printf '\nDASHBOARD_BIND=127.0.0.1:8088\n' >> "$PROJECT_DIR/deploy/dashboard.env"
fi

install -d -m 0700 -o "$APP_UID" -g "$APP_GID" "$PROJECT_DIR/deploy/data"
if [ -e "$PROJECT_DIR/deploy/data/dashboard.sqlite3" ]; then
  chown "$APP_UID:$APP_GID" "$PROJECT_DIR/deploy/data/dashboard.sqlite3"
  chmod 0600 "$PROJECT_DIR/deploy/data/dashboard.sqlite3"
fi

[ -d "$MIHOMO_RULES_DIR" ] || { echo 'Mihomo rules directory is missing' >&2; exit 1; }
chown -R "$APP_UID:$APP_GID" "$MIHOMO_RULES_DIR"
find "$MIHOMO_RULES_DIR" -type d -exec chmod 0770 {} \;
find "$MIHOMO_RULES_DIR" -type f -exec chmod 0660 {} \;

docker compose -f "$PROJECT_DIR/compose.yaml" --env-file "$PROJECT_DIR/deploy/dashboard.env" config --quiet
echo 'PREPARE_RUNTIME=ready'
