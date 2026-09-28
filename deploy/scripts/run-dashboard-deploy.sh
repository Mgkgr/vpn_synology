#!/bin/sh
# Root-only foreground wrapper. It preserves a diagnostic log and a tiny status
# file so a transient SSH reset cannot be mistaken for a failed Docker build.
set -eu

PROJECT_DIR=${PROJECT_DIR:-/volume1/docker/vpn-dashboard}
DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
RUN_ID=${1:?run id is required}

case "$RUN_ID" in *[!A-Za-z0-9-]*|'') echo 'invalid run id' >&2; exit 2 ;; esac
[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }

# The legacy helper does not understand reviewed image pins. Never silently
# replace a worker-managed image with the source tree's older default.
for pin_file in /volume1/docker/vpn-dashboard/compose.versions.yaml /volume1/docker/vpn-dashboard-maintenance/private/image-pins.json; do
  if [ -e "$pin_file" ] || [ -L "$pin_file" ]; then
    echo 'DEPLOYMENT=blocked; use reviewed maintenance deployment for pinned components' >&2
    exit 1
  fi
done

STATUS_FILE="/tmp/vpn-dashboard-deploy-$RUN_ID.status"
LOG_FILE="/tmp/vpn-dashboard-deploy-$RUN_ID.log"
RUN_OWNER=$(stat -c '%U' "$PROJECT_DIR")
umask 077
printf 'RUNNING\n' > "$STATUS_FILE"
touch "$LOG_FILE"
chown "$RUN_OWNER" "$STATUS_FILE" "$LOG_FILE"
chmod 0600 "$STATUS_FILE" "$LOG_FILE"

write_status() {
  printf '%s\n' "$1" > "$STATUS_FILE"
}

# DSM may terminate a foreground child when its SSH transport is lost. Mark
# that outcome explicitly rather than leaving a stale RUNNING state behind.
on_signal() {
  write_status 'FAILED'
  exit 1
}
trap on_signal HUP INT TERM

if /usr/local/sbin/vpn-dashboard-deploy > "$LOG_FILE" 2>&1; then
  write_status 'SUCCESS'
  exit 0
fi

# Compose normally preserves the previous container on a failed replacement;
# start it explicitly as a last safe recovery step and record the result.
if "$DOCKER_BIN" inspect vpn-dashboard >/dev/null 2>&1; then
  "$DOCKER_BIN" start vpn-dashboard >/dev/null 2>&1 || true
fi
write_status 'FAILED'
exit 1
