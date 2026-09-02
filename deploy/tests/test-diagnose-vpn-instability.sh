#!/bin/sh
# The instability snapshot must correlate active dashboard probing with the
# selected fallback and a WireGuard peer without exposing configuration secrets.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
SCRIPT="$ROOT/deploy/scripts/diagnose-vpn-instability.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/volume/mihomo"

cat > "$TMP/id" <<'EOF'
#!/bin/sh
printf '0\n'
EOF
chmod 700 "$TMP/id"

cat > "$TMP/volume/mihomo/config.yaml" <<'EOF'
proxies:
  - name: private-example
    password: must-not-appear
proxy-groups:
  - name: VPS-FALLBACK
    type: fallback
    proxies:
      - WG-IMP
      - HY2-USA
    url: https://www.gstatic.com/generate_204
    interval: 30
    timeout: 5000
    max-failed-times: 3
    lazy: false
rules:
  - MATCH,VPS-FALLBACK
EOF

cat > "$TMP/docker" <<'EOF'
#!/bin/sh
set -eu
case "$1" in
  inspect)
    for last; do :; done
    printf 'CONTAINER=/%s|STATE=running|RUNNING=true|HEALTH=healthy|RESTARTS=0|OOM=false|STARTED=2026-09-03T00:00:00Z\n' "$last"
    ;;
  exec)
    shift
    [ "${1:-}" = -i ] && shift
    container=$1
    shift
    if [ "$container" = vpn-wireguard ] && [ "$1" = wg ]; then
      printf 'server-private\tserver-public\t51820\t0\n'
      printf 'peer-public\tpsk\t198.51.100.1:62000\t10.66.0.4/32\t1000\t100\t200\t25\n'
    elif [ "$container" = vpn-dashboard ]; then
      cat >/dev/null
      cat <<'OUT'
ENABLED_TARGET_COUNT=3
AUTO_DELAY_CONCURRENCY_MAX=6
LAST_PROBE_CYCLE_AT=2026-09-03T00:40:00Z
LAST_PROBE_CYCLE_REQUESTS=6
LAST_PROBE_CYCLE_FAILURES=0
LAST_ROUTE_SWITCH=none
FALLBACK_NOW=WG-IMP
OUT
    fi
    ;;
esac
EOF
chmod 700 "$TMP/docker"

 [ -f "$SCRIPT" ] || { echo 'missing instability diagnostic' >&2; exit 1; }

output=$(PATH="$TMP:$PATH" DOCKER_BIN="$TMP/docker" PROJECT_DIR="$TMP/volume" NOW_EPOCH=1020 /bin/sh "$SCRIPT" 0 10.66.0.4)
printf '%s\n' "$output" | grep -Fqx 'RESULT=success'
printf '%s\n' "$output" | grep -Fqx 'VPN_INSTABILITY_DIAGNOSTIC=read_only'
printf '%s\n' "$output" | grep -Fqx 'FALLBACK_INTERVAL_SECONDS=30'
printf '%s\n' "$output" | grep -Fqx 'FALLBACK_MEMBERS=WG-IMP,HY2-USA'
printf '%s\n' "$output" | grep -Fqx 'ENABLED_TARGET_COUNT=3'
printf '%s\n' "$output" | grep -Fqx 'AUTO_DELAY_CONCURRENCY_MAX=6'
printf '%s\n' "$output" | grep -Fqx 'FALLBACK_NOW=WG-IMP'
printf '%s\n' "$output" | grep -Fqx 'WG_CLIENT=10.66.0.4|HANDSHAKE_AGE_SECONDS=20|SERVER_RX_BYTES=100|SERVER_TX_BYTES=200'
if printf '%s\n' "$output" | grep -F 'must-not-appear' >/dev/null; then
  echo 'the snapshot exposed a configuration secret' >&2
  exit 1
fi

printf '%s\n' 'PASS test-diagnose-vpn-instability'
