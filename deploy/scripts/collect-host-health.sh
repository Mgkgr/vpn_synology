#!/bin/sh
# Lightweight DSM-side telemetry collector. Run it as root once per minute
# from Synology Task Scheduler; it never enters containers or exposes Docker.
set -eu

PROJECT_DIR=${PROJECT_DIR:-/volume1/docker/vpn-dashboard}
DATA_DIR=${DATA_DIR:-$PROJECT_DIR/deploy/data}
OUTPUT_PATH=${OUTPUT_PATH:-$DATA_DIR/host-health.json}
CPU_STATE_PATH=${CPU_STATE_PATH:-$DATA_DIR/.host-health-cpu.state}
DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
APP_UID=${APP_UID:-10001}
APP_GID=${APP_GID:-10001}

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -d /proc ] || { echo '/proc is unavailable' >&2; exit 1; }
mkdir -p "$DATA_DIR"

set -- $(awk '{ print $1, $2, $3 }' /proc/loadavg)
LOAD_ONE=$1
LOAD_FIVE=$2
LOAD_FIFTEEN=$3

meminfo_value() {
  awk -v name="$1" '$1 == name ":" { print $2 * 1024; exit }' /proc/meminfo
}

MEMORY_TOTAL=$(meminfo_value MemTotal)
MEMORY_AVAILABLE=$(meminfo_value MemAvailable)
SWAP_TOTAL=$(meminfo_value SwapTotal)
SWAP_FREE=$(meminfo_value SwapFree)
[ -n "$MEMORY_TOTAL" ] || { echo 'MemTotal is unavailable' >&2; exit 1; }
[ -n "$MEMORY_AVAILABLE" ] || MEMORY_AVAILABLE=0
[ -n "$SWAP_TOTAL" ] || SWAP_TOTAL=0
[ -n "$SWAP_FREE" ] || SWAP_FREE=0

CPU_LINE=$(awk '/^cpu / { total = 0; for (i = 2; i <= NF; i++) total += $i; idle = $5 + $6; print total, idle; exit }' /proc/stat)
set -- $CPU_LINE
CPU_TOTAL=$1
CPU_IDLE=$2
CPU_USAGE=null
if [ -f "$CPU_STATE_PATH" ]; then
  set -- $(cat "$CPU_STATE_PATH" 2>/dev/null || true)
  if [ "$#" -eq 2 ]; then
    PREVIOUS_TOTAL=$1
    PREVIOUS_IDLE=$2
    CPU_USAGE=$(awk -v total="$CPU_TOTAL" -v idle="$CPU_IDLE" -v previous_total="$PREVIOUS_TOTAL" -v previous_idle="$PREVIOUS_IDLE" 'BEGIN { delta_total = total - previous_total; delta_idle = idle - previous_idle; if (delta_total > 0) printf "%.2f", (delta_total - delta_idle) * 100 / delta_total; else print "null" }')
  fi
fi
printf '%s %s\n' "$CPU_TOTAL" "$CPU_IDLE" > "$CPU_STATE_PATH"
chmod 0600 "$CPU_STATE_PATH"

set -- $(df -kP /volume1 | awk 'NR == 2 { print $2 * 1024, $4 * 1024 }')
VOLUME_TOTAL=${1:-0}
VOLUME_AVAILABLE=${2:-0}

NETWORK=$(awk '
  FNR == 1 { next }
  {
    rx_errors += $4; rx_dropped += $5; tx_errors += $12; tx_dropped += $13
  }
  END { printf "%d %d %d %d", rx_errors, rx_dropped, tx_errors, tx_dropped }
' /proc/net/dev)
set -- $NETWORK
RX_ERRORS=${1:-0}
RX_DROPPED=${2:-0}
TX_ERRORS=${3:-0}
TX_DROPPED=${4:-0}

container_json() {
  logical_name=$1
  shift
  actual_name=''
  for candidate in "$@"; do
    if "$DOCKER_BIN" inspect "$candidate" >/dev/null 2>&1; then
      actual_name=$candidate
      break
    fi
  done

  if [ -z "$actual_name" ]; then
    printf '{"name":"%s","state":"absent","restart_count":0,"health":null}' "$logical_name"
    return
  fi

  state=$("$DOCKER_BIN" inspect --format '{{.State.Status}}' "$actual_name" 2>/dev/null || printf unknown)
  restart_count=$("$DOCKER_BIN" inspect --format '{{.RestartCount}}' "$actual_name" 2>/dev/null || printf 0)
  health=$("$DOCKER_BIN" inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$actual_name" 2>/dev/null || printf none)
  case "$state" in running|created|restarting|paused|exited|dead) ;; *) state=unknown ;; esac
  case "$restart_count" in ''|*[!0-9]*) restart_count=0 ;; esac
  case "$health" in healthy|unhealthy|starting) health_json="\"$health\"" ;; *) health_json=null ;; esac
  printf '{"name":"%s","state":"%s","restart_count":%s,"health":%s}' "$logical_name" "$state" "$restart_count" "$health_json"
}

TMP_PATH=$(mktemp "$DATA_DIR/.host-health.json.XXXXXX")
trap 'rm -f "$TMP_PATH"' EXIT INT TERM
{
  printf '{'
  printf '"observed_at":"%s",' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf '"cpu_usage_percent":%s,' "$CPU_USAGE"
  printf '"load":{"one":%s,"five":%s,"fifteen":%s},' "$LOAD_ONE" "$LOAD_FIVE" "$LOAD_FIFTEEN"
  printf '"memory":{"total_bytes":%s,"available_bytes":%s,"swap_total_bytes":%s,"swap_free_bytes":%s},' "$MEMORY_TOTAL" "$MEMORY_AVAILABLE" "$SWAP_TOTAL" "$SWAP_FREE"
  printf '"volume":{"total_bytes":%s,"available_bytes":%s},' "$VOLUME_TOTAL" "$VOLUME_AVAILABLE"
  printf '"network":{"rx_errors":%s,"rx_dropped":%s,"tx_errors":%s,"tx_dropped":%s},' "$RX_ERRORS" "$RX_DROPPED" "$TX_ERRORS" "$TX_DROPPED"
  printf '"containers":['
  container_json dashboard vpn-dashboard
  printf ','
  container_json wireguard vpn-wireguard wireguard
  printf ','
  container_json mihomo vpn-mihomo mihomo
  printf ','
  container_json metacubexd vpn-metacubexd metacubexd
  printf ','
  container_json uptime-kuma vpn-uptime-kuma uptime-kuma
  printf ']}'
} > "$TMP_PATH"

chown "$APP_UID:$APP_GID" "$TMP_PATH"
chmod 0640 "$TMP_PATH"
mv -f "$TMP_PATH" "$OUTPUT_PATH"
trap - EXIT INT TERM
