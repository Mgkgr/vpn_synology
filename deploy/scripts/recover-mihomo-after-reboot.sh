#!/bin/sh
# Recover only Mihomo after a NAS reboot.  Mihomo intentionally shares the
# WireGuard network namespace; attaching it to a regular Docker network would
# break the gateway, so this script refuses such a configuration.
set -eu

DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
WIREGUARD_CONTAINER=${WIREGUARD_CONTAINER:-vpn-wireguard}
MIHOMO_CONTAINER=${MIHOMO_CONTAINER:-vpn-mihomo}
DASHBOARD_CONTAINER=${DASHBOARD_CONTAINER:-vpn-dashboard}

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }
for container in "$WIREGUARD_CONTAINER" "$MIHOMO_CONTAINER" "$DASHBOARD_CONTAINER"; do
  "$DOCKER_BIN" inspect "$container" >/dev/null 2>&1 || { echo "required container is missing: $container" >&2; exit 1; }
done

is_running() {
  [ "$("$DOCKER_BIN" inspect -f '{{.State.Running}}' "$1")" = true ]
}

echo 'RECOVERY=begin'
if ! is_running "$WIREGUARD_CONTAINER"; then
  echo 'WIREGUARD_START=needed'
  "$DOCKER_BIN" start "$WIREGUARD_CONTAINER" >/dev/null
else
  echo 'WIREGUARD_START=not_needed'
fi

attempt=0
while ! is_running "$WIREGUARD_CONTAINER"; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 20 ] || { echo 'REASON=WireGuard did not become ready' >&2; exit 1; }
  sleep 1
done

wireguard_id=$("$DOCKER_BIN" inspect -f '{{.Id}}' "$WIREGUARD_CONTAINER")
network_mode=$("$DOCKER_BIN" inspect -f '{{.HostConfig.NetworkMode}}' "$MIHOMO_CONTAINER")
case "$network_mode" in
  "container:$wireguard_id"|"container:$WIREGUARD_CONTAINER")
    echo 'MIHOMO_NETWORK_NAMESPACE=wireguard'
    ;;
  *)
    echo "MIHOMO_NETWORK_NAMESPACE=unexpected:$network_mode" >&2
    echo 'REASON=refusing to attach Mihomo to a different network' >&2
    exit 2
    ;;
esac

if is_running "$MIHOMO_CONTAINER"; then
  echo 'MIHOMO_START=not_needed'
else
  echo 'MIHOMO_START=needed'
  if ! "$DOCKER_BIN" start "$MIHOMO_CONTAINER" >/dev/null; then
    echo 'MIHOMO_START=failed' >&2
    "$DOCKER_BIN" inspect -f 'MIHOMO_STATE={{.State.Status}}|EXIT={{.State.ExitCode}}|ERROR={{.State.Error}}' "$MIHOMO_CONTAINER" >&2
    exit 1
  fi
fi

attempt=0
while ! is_running "$MIHOMO_CONTAINER"; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 20 ] || { echo 'REASON=Mihomo did not become ready' >&2; exit 1; }
  sleep 1
done

if "$DOCKER_BIN" exec "$MIHOMO_CONTAINER" /mihomo -t -d /root/.config/mihomo >/dev/null 2>&1; then
  echo 'MIHOMO_CONFIG=valid'
else
  echo 'MIHOMO_CONFIG=invalid' >&2
  exit 1
fi

if "$DOCKER_BIN" exec -i "$DASHBOARD_CONTAINER" python - <<'PY'
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
then
  echo 'MIHOMO_CONTROLLER=ready'
else
  echo 'MIHOMO_CONTROLLER=unavailable' >&2
  exit 1
fi

echo 'RECOVERY=success'
