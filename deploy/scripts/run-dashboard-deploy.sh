#!/bin/sh
# Root-only detached wrapper. It preserves a diagnostic log and a tiny status
# file so a transient SSH reset cannot be mistaken for a failed Docker build.
set -eu

PROJECT_DIR=${PROJECT_DIR:-/volume1/docker/vpn-dashboard}
DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
RUN_ID=${1:?run id is required}

case "$RUN_ID" in *[!A-Za-z0-9-]*|'') echo 'invalid run id' >&2; exit 2 ;; esac
[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }

STATUS_FILE="/tmp/vpn-dashboard-deploy-$RUN_ID.status"
LOG_FILE="/tmp/vpn-dashboard-deploy-$RUN_ID.log"
RUN_OWNER=$(stat -c '%U' "$PROJECT_DIR")
umask 077
printf 'RUNNING\n' > "$STATUS_FILE"
touch "$LOG_FILE"
chown "$RUN_OWNER" "$STATUS_FILE" "$LOG_FILE"
chmod 0600 "$STATUS_FILE" "$LOG_FILE"

if /usr/local/sbin/vpn-dashboard-deploy > "$LOG_FILE" 2>&1; then
  printf 'SUCCESS\n' > "$STATUS_FILE"
  exit 0
fi

# Compose normally preserves the previous container on a failed replacement;
# start it explicitly as a last safe recovery step and record the result.
if "$DOCKER_BIN" inspect vpn-dashboard >/dev/null 2>&1; then
  "$DOCKER_BIN" start vpn-dashboard >/dev/null 2>&1 || true
fi
printf 'FAILED\n' > "$STATUS_FILE"
exit 1
