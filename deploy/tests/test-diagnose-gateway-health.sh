#!/bin/sh
# The comprehensive health check must expose only operational summaries.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
SCRIPT="$ROOT/deploy/scripts/diagnose-gateway-health.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/volume"

cat > "$TMP/id" <<'EOF'
#!/bin/sh
printf '0\n'
EOF
chmod 700 "$TMP/id"

cat > "$TMP/btrfs" <<'EOF'
#!/bin/sh
case "$1" in
  scrub) printf 'scrub status: finished, no errors found\n' ;;
  device) printf '[/dev/sda].write_io_errs 0\n' ;;
esac
EOF
chmod 700 "$TMP/btrfs"

cat > "$TMP/docker" <<'EOF'
#!/bin/sh
set -eu
case "$1" in
  inspect)
    for last; do :; done
    printf 'CONTAINER=/%s|STATE=running|RUNNING=true|HEALTH=healthy|RESTARTS=0|OOM=false|ERROR=none\n' "$last"
    ;;
  stats)
    printf 'RESOURCE=vpn-wireguard|CPU=0.10%%|MEM=20MiB / 512MiB|NET=1kB / 2kB|PIDS=2\n'
    ;;
  port)
    printf '0.0.0.0:51820\n'
    ;;
  logs)
    exit 0
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
      printf 'MIHOMO_CONTROLLER=ok|GROUPS=2|RULE_PROVIDERS=5\n'
    fi
    ;;
esac
EOF
chmod 700 "$TMP/docker"

output=$(PATH="$TMP:$PATH" \
  DOCKER_BIN="$TMP/docker" \
  BTRFS_BIN="$TMP/btrfs" \
  VOLUME="$TMP/volume" \
  NOW_EPOCH=1020 \
  /bin/sh "$SCRIPT")

printf '%s\n' "$output" | grep -Fqx 'RESULT=success'
printf '%s\n' "$output" | grep -Fqx 'GATEWAY_HEALTH_FORMAT=v1'
printf '%s\n' "$output" | grep -Fqx 'UDP_51820=published'
printf '%s\n' "$output" | grep -Fqx 'WG_PEERS_TOTAL=1'
printf '%s\n' "$output" | grep -Fqx 'WG_PEERS_FRESH_5_MINUTES=1'
printf '%s\n' "$output" | grep -Fqx 'MIHOMO_CONTROLLER=ok|GROUPS=2|RULE_PROVIDERS=5'
printf '%s\n' "$output" | grep -Fqx 'CONTAINER=/vpn-wireguard|STATE=running|RUNNING=true|HEALTH=healthy|RESTARTS=0|OOM=false|ERROR=none'
printf '%s\n' 'PASS test-diagnose-gateway-health'
