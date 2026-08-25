#!/bin/sh
# Apply Docker health checks to Mihomo and the standalone MetaCubeXD panel.
# Only the two affected containers are recreated; WireGuard and client data are
# never recreated, modified or printed.  A known current compose hash prevents
# overwriting unrelated edits made on the NAS.
set -eu

SOURCE_COMPOSE=${1:?source compose path is required}
PROJECT_DIR=${PROJECT_DIR:-/volume1/docker/vpn-gateway}
TARGET_COMPOSE="$PROJECT_DIR/compose.yaml"
BACKUP_DIR="$PROJECT_DIR/backups"
DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
EXPECTED_COMPOSE_SHA256=${EXPECTED_COMPOSE_SHA256:-16de6fbf949f9abeee0a723fbb019f3328da45fff4b5febe3a6d733c18e41903}
MIHOMO_CONTAINER=${MIHOMO_CONTAINER:-vpn-mihomo}
METACUBEXD_CONTAINER=${METACUBEXD_CONTAINER:-vpn-metacubexd}
DASHBOARD_CONTAINER=${DASHBOARD_CONTAINER:-vpn-dashboard}
WAIT_SECONDS=${WAIT_SECONDS:-90}

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }
[ -r "$SOURCE_COMPOSE" ] || { echo "source compose is unreadable: $SOURCE_COMPOSE" >&2; exit 1; }
[ -f "$TARGET_COMPOSE" ] || { echo "gateway compose is missing: $TARGET_COMPOSE" >&2; exit 1; }

current_hash=$(sha256sum "$TARGET_COMPOSE" | awk '{print $1}')
if [ "$current_hash" != "$EXPECTED_COMPOSE_SHA256" ]; then
  echo 'REASON=gateway compose differs from the reviewed version; no change was made' >&2
  echo "CURRENT_COMPOSE_SHA256=$current_hash" >&2
  exit 2
fi

tmp_compose="$PROJECT_DIR/.compose.healthcheck.$$.yaml"
backup_compose=''
cleanup() {
  rm -f "$tmp_compose"
}
trap cleanup EXIT HUP INT TERM

validate_source() {
  grep -Fqx 'name: vpn-gateway' "$1" &&
    grep -Fq 'image: metacubex/mihomo@sha256:e6acd921addecfd59a8e2d38203f88356d635b54de6c0673db0e015139989312' "$1" &&
    grep -Fq 'image: ghcr.io/metacubex/metacubexd@sha256:ec27292e8d26dd561221c3ddf8d1fc7bab5452861362bd945a3b83555c295676' "$1" &&
    grep -Fq "grep -Eq '^[[:space:]]*[0-9]+: [0-9A-F]{8,32}:2393 '" "$1" &&
    grep -Fq "const http=require('http')" "$1"
}

mihomo_socket_probe() {
  "$DOCKER_BIN" exec "$MIHOMO_CONTAINER" /bin/sh -ec "
    test -s /proc/1/cmdline &&
    grep -Eq '^[[:space:]]*[0-9]+: [0-9A-F]{8,32}:2393 ' /proc/net/tcp /proc/net/tcp6 2>/dev/null
  "
}

metacubexd_http_probe() {
  "$DOCKER_BIN" exec "$METACUBEXD_CONTAINER" node -e "
    const http=require('http');
    const request=http.get({host:'127.0.0.1',port:80,path:'/',timeout:3000},response=>{
      response.resume();
      process.exit(response.statusCode>=200&&response.statusCode<400?0:1);
    });
    request.on('timeout',()=>request.destroy(new Error('timeout')));
    request.on('error',()=>process.exit(1));
  "
}

mihomo_controller_probe() {
  "$DOCKER_BIN" exec -i "$DASHBOARD_CONTAINER" python - <<'PY'
import os
from pathlib import Path
from urllib.request import Request, urlopen

url = os.environ.get("MIHOMO_URL", "http://vpn-wireguard:9091").rstrip("/") + "/version"
headers = {}
secret_file = os.environ.get("MIHOMO_API_SECRET_FILE")
if secret_file:
    secret = Path(secret_file).read_text(encoding="utf-8").strip()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
with urlopen(Request(url, headers=headers), timeout=5) as response:
    if not 200 <= response.status < 300:
        raise RuntimeError("Mihomo controller returned a non-success status")
PY
}

wait_for_healthy() {
  container=$1
  elapsed=0
  while [ "$elapsed" -lt "$WAIT_SECONDS" ]; do
    state=$("$DOCKER_BIN" inspect -f '{{.State.Status}}' "$container" 2>/dev/null || echo missing)
    health=$("$DOCKER_BIN" inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container" 2>/dev/null || echo none)
    if [ "$state" = running ] && [ "$health" = healthy ]; then
      return 0
    fi
    if [ "$state" = exited ] || [ "$state" = dead ] || [ "$health" = unhealthy ]; then
      echo "CONTAINER=$container|STATE=$state|HEALTH=$health" >&2
      return 1
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  echo "CONTAINER=$container|STATE=$state|HEALTH=$health|WAIT=timeout" >&2
  return 1
}

restore_backup() {
  reason=$1
  echo "REASON=$reason; restoring the previous compose" >&2
  if [ -n "$backup_compose" ] && [ -f "$backup_compose" ]; then
    cp "$backup_compose" "$tmp_compose"
    if (cd "$PROJECT_DIR" && "$DOCKER_BIN" compose -f "$tmp_compose" config --quiet); then
      mv "$tmp_compose" "$TARGET_COMPOSE"
      (cd "$PROJECT_DIR" && "$DOCKER_BIN" compose up -d --no-deps --force-recreate mihomo metacubexd) || true
      echo "ROLLBACK=applied:$backup_compose" >&2
    else
      echo 'ROLLBACK=failed_to_validate_backup' >&2
    fi
  fi
  exit 1
}

validate_source "$SOURCE_COMPOSE" || { echo 'REASON=source compose did not contain the reviewed health checks' >&2; exit 1; }
for container in "$MIHOMO_CONTAINER" "$METACUBEXD_CONTAINER" "$DASHBOARD_CONTAINER"; do
  "$DOCKER_BIN" inspect "$container" >/dev/null 2>&1 || { echo "required container is missing: $container" >&2; exit 1; }
done

"$DOCKER_BIN" exec "$MIHOMO_CONTAINER" /mihomo -t -d /root/.config/mihomo >/dev/null || {
  echo 'REASON=Mihomo configuration is not valid; no change was made' >&2
  exit 1
}
mihomo_socket_probe || { echo 'REASON=Mihomo controller socket is not ready; no change was made' >&2; exit 1; }
metacubexd_http_probe || { echo 'REASON=MetaCubeXD HTTP probe failed; no change was made' >&2; exit 1; }
mihomo_controller_probe || { echo 'REASON=Mihomo authenticated controller probe failed; no change was made' >&2; exit 1; }

mkdir -p "$BACKUP_DIR"
timestamp=$(date +%Y%m%d-%H%M%S)
backup_compose="$BACKUP_DIR/compose.yaml.before-healthchecks.$timestamp"
cp "$TARGET_COMPOSE" "$backup_compose"
chown root:root "$backup_compose"
chmod 0600 "$backup_compose"
cp "$SOURCE_COMPOSE" "$tmp_compose"
chown root:root "$tmp_compose"
chmod 0600 "$tmp_compose"
(cd "$PROJECT_DIR" && "$DOCKER_BIN" compose -f "$tmp_compose" config --quiet) || restore_backup 'staged compose validation failed'
mv "$tmp_compose" "$TARGET_COMPOSE"

(cd "$PROJECT_DIR" && "$DOCKER_BIN" compose up -d --no-deps --force-recreate mihomo metacubexd) || restore_backup 'container recreation failed'
wait_for_healthy "$MIHOMO_CONTAINER" || restore_backup 'Mihomo did not become healthy'
wait_for_healthy "$METACUBEXD_CONTAINER" || restore_backup 'MetaCubeXD did not become healthy'
mihomo_socket_probe || restore_backup 'Mihomo controller socket did not recover'
metacubexd_http_probe || restore_backup 'MetaCubeXD HTTP probe did not recover'
mihomo_controller_probe || restore_backup 'Mihomo authenticated controller did not recover'

echo 'MIHOMO_CONTROLLER=ready'
echo 'MIHOMO_HEALTHCHECK=healthy'
echo 'METACUBEXD_HEALTHCHECK=healthy'
echo "BACKUP=$backup_compose"
echo 'HEALTHCHECK_APPLY=success'
