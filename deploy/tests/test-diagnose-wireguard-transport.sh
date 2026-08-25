#!/bin/sh
# Regression coverage for the sanitised WireGuard counter watch.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
SCRIPT="$ROOT/deploy/scripts/diagnose-wireguard-transport.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/id" <<'EOF'
#!/bin/sh
printf '0\n'
EOF
chmod 700 "$TMP/id"

cat > "$TMP/docker" <<'EOF'
#!/bin/sh
set -eu
if [ "$1" = inspect ]; then
  exit 0
fi
if [ "$1" = port ]; then
  printf '0.0.0.0:51820\n'
  exit 0
fi
if [ "$1" = exec ]; then
  shift
  [ "$1" = vpn-wireguard ]
  shift
  if [ "$1" = wg ]; then
    count=0
    [ -f "$DUMP_COUNT" ] && count=$(cat "$DUMP_COUNT")
    printf '%s' $((count + 1)) > "$DUMP_COUNT"
    printf 'server-private\tserver-public\t51820\t0\n'
    if [ "$count" = 0 ]; then
      printf 'peer-public\tpsk\t198.51.100.1:62000\t10.66.0.4/32\t1000\t100\t200\t25\n'
      printf 'peer-public-2\tpsk\t198.51.100.2:62000\t10.66.0.5/32\t1000\t500\t700\t25\n'
    else
      # wg-easy can reorder peers between reads; the same address must retain
      # its identity for counter deltas.
      printf 'peer-public-2\tpsk\t198.51.100.2:62000\t10.66.0.5/32\t1010\t800\t900\t25\n'
      printf 'peer-public\tpsk\t198.51.100.1:62000\t10.66.0.4/32\t1010\t300\t500\t25\n'
    fi
    exit 0
  fi
  if [ "$1" = sh ]; then
    printf 'net.ipv4.ip_forward = 1\n'
    exit 0
  fi
fi
exit 64
EOF
chmod 700 "$TMP/docker"

output=$(PATH="$TMP:$PATH" \
  DUMP_COUNT="$TMP/dump-count" \
  DOCKER_BIN="$TMP/docker" \
  NOW_EPOCH=1020 \
  /bin/sh "$SCRIPT" 0)

printf '%s\n' "$output" | grep -Fqx 'RESULT=success'
printf '%s\n' "$output" | grep -Fqx 'UDP_51820=published'
printf '%s\n' "$output" | grep -Fqx 'PEERS_TOTAL=2'
printf '%s\n' "$output" | grep -Fqx 'PEER_10.66.0.4=HANDSHAKE_AGE_SECONDS=20|ENDPOINT=seen|SERVER_RX_BYTES=100|SERVER_TX_BYTES=200'
printf '%s\n' "$output" | grep -Fqx 'PEER_10.66.0.4_DELTA=HANDSHAKE_CHANGED=yes|SERVER_RX_DELTA=200|SERVER_TX_DELTA=300'
printf '%s\n' "$output" | grep -Fqx 'PEER_10.66.0.5_DELTA=HANDSHAKE_CHANGED=yes|SERVER_RX_DELTA=300|SERVER_TX_DELTA=200'
printf '%s\n' 'PASS test-diagnose-wireguard-transport'
