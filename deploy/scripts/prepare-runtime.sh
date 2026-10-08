#!/bin/sh
set -eu

# Run as root on the NAS immediately before vpn-dashboard-deploy.
# This script never touches WireGuard, Mihomo or their running containers.
PROJECT_DIR=${PROJECT_DIR:-/volume1/docker/vpn-dashboard}
MIHOMO_RULES_DIR=${MIHOMO_RULES_DIR:-/volume1/docker/vpn-gateway/mihomo/rules}
APP_UID=${APP_UID:-10001}
APP_GID=${APP_GID:-10001}
DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
SECRETS_DIR=${SECRETS_DIR:-$PROJECT_DIR/deploy/secrets}

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }
[ -f "$PROJECT_DIR/deploy/dashboard.env" ] || { echo 'dashboard.env is missing' >&2; exit 1; }
# /run is ephemeral after reboot. Keep the directory inode while mounted;
# never enable the worker or an Auto policy as part of dashboard deployment.
/usr/bin/python3 "$PROJECT_DIR/deploy/scripts/prepare-maintenance-socket.py"
install -d -m 0700 -o "$APP_UID" -g "$APP_GID" "$SECRETS_DIR"

# Migrate the two runtime secrets out of dashboard.env exactly once. The
# dashboard is non-root, so it owns its 0600 files and can only read them via
# the read-only /run/secrets mount.
DASHBOARD_SECRET_FILE="$SECRETS_DIR/dashboard_encryption_key"
DASHBOARD_FILE_SETTING=$(sed -n 's/^DASHBOARD_ENCRYPTION_KEY_FILE=//p' "$PROJECT_DIR/deploy/dashboard.env" | head -n 1)
DASHBOARD_LEGACY_SETTING=$(sed -n 's/^DASHBOARD_ENCRYPTION_KEY=//p' "$PROJECT_DIR/deploy/dashboard.env" | head -n 1)
if [ -n "$DASHBOARD_FILE_SETTING" ] && [ "$DASHBOARD_FILE_SETTING" != /run/secrets/dashboard_encryption_key ]; then
  echo 'DASHBOARD_ENCRYPTION_KEY_FILE has an unexpected path' >&2
  exit 1
fi
if [ ! -f "$DASHBOARD_SECRET_FILE" ]; then
  [ -n "$DASHBOARD_LEGACY_SETTING" ] || { echo 'dashboard encryption secret is missing' >&2; exit 1; }
  umask 077
  printf '%s' "$DASHBOARD_LEGACY_SETTING" > "$DASHBOARD_SECRET_FILE"
fi
chown "$APP_UID:$APP_GID" "$DASHBOARD_SECRET_FILE"
chmod 0600 "$DASHBOARD_SECRET_FILE"
sed -i '/^DASHBOARD_ENCRYPTION_KEY=/d' "$PROJECT_DIR/deploy/dashboard.env"
if [ -z "$DASHBOARD_FILE_SETTING" ]; then
  printf '\nDASHBOARD_ENCRYPTION_KEY_FILE=/run/secrets/dashboard_encryption_key\n' >> "$PROJECT_DIR/deploy/dashboard.env"
fi

MIHOMO_SECRET_FILE="$SECRETS_DIR/mihomo_api_secret"
MIHOMO_FILE_SETTING=$(sed -n 's/^MIHOMO_API_SECRET_FILE=//p' "$PROJECT_DIR/deploy/dashboard.env" | head -n 1)
MIHOMO_LEGACY_SETTING=$(sed -n 's/^MIHOMO_API_SECRET=//p' "$PROJECT_DIR/deploy/dashboard.env" | head -n 1)
if [ -n "$MIHOMO_FILE_SETTING" ] && [ "$MIHOMO_FILE_SETTING" != /run/secrets/mihomo_api_secret ]; then
  echo 'MIHOMO_API_SECRET_FILE has an unexpected path' >&2
  exit 1
fi
if [ -n "$MIHOMO_LEGACY_SETTING" ]; then
  if [ ! -f "$MIHOMO_SECRET_FILE" ]; then
    umask 077
    printf '%s' "$MIHOMO_LEGACY_SETTING" > "$MIHOMO_SECRET_FILE"
  fi
  chown "$APP_UID:$APP_GID" "$MIHOMO_SECRET_FILE"
  chmod 0600 "$MIHOMO_SECRET_FILE"
  sed -i '/^MIHOMO_API_SECRET=/d' "$PROJECT_DIR/deploy/dashboard.env"
  if [ -z "$MIHOMO_FILE_SETTING" ]; then
    printf 'MIHOMO_API_SECRET_FILE=/run/secrets/mihomo_api_secret\n' >> "$PROJECT_DIR/deploy/dashboard.env"
  fi
elif [ -n "$MIHOMO_FILE_SETTING" ]; then
  [ -f "$MIHOMO_SECRET_FILE" ] || { echo 'Mihomo controller secret file is missing' >&2; exit 1; }
  chown "$APP_UID:$APP_GID" "$MIHOMO_SECRET_FILE"
  chmod 0600 "$MIHOMO_SECRET_FILE"
fi
# Preserve all secrets and integration URLs; only remove the direct LAN bind.
if grep -q '^DASHBOARD_BIND=' "$PROJECT_DIR/deploy/dashboard.env"; then
  sed -i 's|^DASHBOARD_BIND=.*$|DASHBOARD_BIND=127.0.0.1:8088|' "$PROJECT_DIR/deploy/dashboard.env"
else
  printf '\nDASHBOARD_BIND=127.0.0.1:8088\n' >> "$PROJECT_DIR/deploy/dashboard.env"
fi
# GeoData is mounted as two read-only assets rather than the Mihomo directory,
# which keeps controller configuration and outbound credentials out of Dashboard.
if grep -q '^GEODATA_DIR=' "$PROJECT_DIR/deploy/dashboard.env"; then
  sed -i 's|^GEODATA_DIR=.*$|GEODATA_DIR=/geodata|' "$PROJECT_DIR/deploy/dashboard.env"
else
  printf 'GEODATA_DIR=/geodata\n' >> "$PROJECT_DIR/deploy/dashboard.env"
fi

install -d -m 0700 -o "$APP_UID" -g "$APP_GID" "$PROJECT_DIR/deploy/data"
if [ -e "$PROJECT_DIR/deploy/data/dashboard.sqlite3" ]; then
  chown "$APP_UID:$APP_GID" "$PROJECT_DIR/deploy/data/dashboard.sqlite3"
  chmod 0600 "$PROJECT_DIR/deploy/data/dashboard.sqlite3"
fi

[ -d "$MIHOMO_RULES_DIR" ] || { echo 'Mihomo rules directory is missing' >&2; exit 1; }
[ -r "$(dirname "$MIHOMO_RULES_DIR")/GeoIP.dat" ] || { echo 'Mihomo GeoIP.dat is missing' >&2; exit 1; }
[ -r "$(dirname "$MIHOMO_RULES_DIR")/GeoSite.dat" ] || { echo 'Mihomo GeoSite.dat is missing' >&2; exit 1; }
MIHOMO_DIR=$(dirname "$MIHOMO_RULES_DIR")
DASHBOARD_UID="$APP_UID" MIHOMO_DIR="$MIHOMO_DIR" /bin/sh "$PROJECT_DIR/deploy/scripts/ensure-geodata-read-access.sh"
chown -R "$APP_UID:$APP_GID" "$MIHOMO_RULES_DIR"
find "$MIHOMO_RULES_DIR" -type d -exec chmod 0770 {} \;
find "$MIHOMO_RULES_DIR" -type f -exec chmod 0660 {} \;
# A rules provider may be represented by a symlink.  Recursive chown/find
# intentionally do not dereference it, so repair its target explicitly while
# refusing anything outside the dedicated Mihomo rules directory.
RULES_ROOT=$(cd "$MIHOMO_RULES_DIR" && pwd -P)
find "$MIHOMO_RULES_DIR" -type l | while IFS= read -r link; do
  target=$(readlink -f "$link") || { echo "unresolvable rules symlink: $link" >&2; exit 1; }
  case "$target" in
    "$RULES_ROOT"/*) ;;
    *) echo "rules symlink escapes the VPN project: $link" >&2; exit 1 ;;
  esac
  if [ -d "$target" ]; then
    chown -R "$APP_UID:$APP_GID" "$target"
    find "$target" -type d -exec chmod 0770 {} \;
    find "$target" -type f -exec chmod 0660 {} \;
  elif [ -f "$target" ]; then
    chown "$APP_UID:$APP_GID" "$target"
    chmod 0660 "$target"
  else
    echo "unsupported rules symlink target: $link" >&2
    exit 1
  fi
done

"$DOCKER_BIN" compose -f "$PROJECT_DIR/compose.yaml" --env-file "$PROJECT_DIR/deploy/dashboard.env" config --quiet
echo 'PREPARE_RUNTIME=ready'
