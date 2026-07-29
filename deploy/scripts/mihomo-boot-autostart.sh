#!/bin/sh
# Run from DSM Task Scheduler as root on the Boot-up event. Docker restarts
# containers independently, so its restart policy cannot order a container
# that shares WireGuard's network namespace after its namespace owner.
set -eu

DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
WIREGUARD_CONTAINER=${WIREGUARD_CONTAINER:-vpn-wireguard}
MIHOMO_CONTAINER=${MIHOMO_CONTAINER:-vpn-mihomo}
MAX_WAIT_SECONDS=${MAX_WAIT_SECONDS:-180}
RETRY_DELAY_SECONDS=${RETRY_DELAY_SECONDS:-1}
LOG_FILE=${LOG_FILE:-/volume1/docker/vpn-gateway/logs/mihomo-boot-autostart.log}

case "$MAX_WAIT_SECONDS" in ''|*[!0-9]*) echo 'MAX_WAIT_SECONDS must be a non-negative integer' >&2; exit 1 ;; esac
case "$RETRY_DELAY_SECONDS" in ''|0|*[!0-9]*) echo 'RETRY_DELAY_SECONDS must be a positive integer' >&2; exit 1 ;; esac

mkdir -p "$(dirname "$LOG_FILE")"
umask 077
exec >> "$LOG_FILE" 2>&1

is_running() {
  running=$("$DOCKER_BIN" inspect -f '{{.State.Running}}' "$1" 2>/dev/null) || return 1
  [ "$running" = true ]
}

wait_until() {
  description=$1
  shift
  elapsed=0
  while ! "$@"; do
    if [ "$elapsed" -ge "$MAX_WAIT_SECONDS" ]; then
      echo "REASON=$description did not become ready within ${MAX_WAIT_SECONDS}s" >&2
      return 1
    fi
    sleep "$RETRY_DELAY_SECONDS"
    elapsed=$((elapsed + RETRY_DELAY_SECONDS))
  done
}

container_exists() {
  "$DOCKER_BIN" inspect "$1" >/dev/null 2>&1
}

wait_until_running() {
  container=$1
  wait_until "$container" is_running "$container"
}

echo "MIHOMO_AUTOSTART=begin|at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
[ -x "$DOCKER_BIN" ] || { echo "REASON=Docker binary unavailable: $DOCKER_BIN" >&2; exit 1; }
for container in "$WIREGUARD_CONTAINER" "$MIHOMO_CONTAINER"; do
  wait_until "$container definition" container_exists "$container"
done

if ! is_running "$WIREGUARD_CONTAINER"; then
  echo 'WIREGUARD_START=needed'
  "$DOCKER_BIN" start "$WIREGUARD_CONTAINER" >/dev/null
else
  echo 'WIREGUARD_START=not_needed'
fi
wait_until_running "$WIREGUARD_CONTAINER"

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

if ! is_running "$MIHOMO_CONTAINER"; then
  echo 'MIHOMO_START=needed'
  "$DOCKER_BIN" start "$MIHOMO_CONTAINER" >/dev/null
else
  echo 'MIHOMO_START=not_needed'
fi
wait_until_running "$MIHOMO_CONTAINER"

echo 'MIHOMO_AUTOSTART=success'
