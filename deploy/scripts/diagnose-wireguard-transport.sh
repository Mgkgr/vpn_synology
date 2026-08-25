#!/bin/sh
# Read-only WireGuard transport observation. Public keys and endpoint addresses
# are intentionally not printed.
set -eu

DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
WIREGUARD_CONTAINER=${WIREGUARD_CONTAINER:-vpn-wireguard}
INTERFACE=${INTERFACE:-wg0}
WATCH_SECONDS=${1:-45}
NOW_EPOCH=${NOW_EPOCH:-}

case "$WATCH_SECONDS" in ''|*[!0-9]*) echo 'watch duration must be an integer number of seconds' >&2; exit 1 ;; esac
[ "$WATCH_SECONDS" -le 60 ] || { echo 'watch duration must not exceed 60 seconds' >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }
"$DOCKER_BIN" inspect "$WIREGUARD_CONTAINER" >/dev/null 2>&1 || {
  echo "WireGuard container is unavailable: $WIREGUARD_CONTAINER" >&2
  exit 1
}
if [ -z "$NOW_EPOCH" ]; then
  NOW_EPOCH=$(date +%s)
fi
case "$NOW_EPOCH" in ''|*[!0-9]*) echo 'current time is unavailable' >&2; exit 1 ;; esac

TMP=$(mktemp -d /tmp/wg-transport.XXXXXX)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

snapshot() {
  output=$1
  "$DOCKER_BIN" exec "$WIREGUARD_CONTAINER" wg show "$INTERFACE" dump | awk -F '\t' -v now="$NOW_EPOCH" '
    NR == 1 { next }
    NF < 7 { next }
    $5 ~ /^[0-9]+$/ && $6 ~ /^[0-9]+$/ && $7 ~ /^[0-9]+$/ {
      allowed = $4
      split(allowed, allowed_parts, ",")
      client = allowed_parts[1]
      sub(/\/32$/, "", client)
      if (client !~ /^10\.66\.[0-9]+\.[0-9]+$/) {
        count += 1
        client = "unknown-" count
      }
      endpoint = ($3 == "" || $3 == "(none)") ? "none" : "seen"
      handshake = $5 == 0 ? "never" : (now >= $5 ? now - $5 : 0)
      printf "%s|%s|%s|%s|%s\n", client, handshake, endpoint, $6, $7
    }
    END { if (count == 0) exit 0 }
  ' > "$output"
}

print_snapshot() {
  file=$1
  if [ ! -s "$file" ]; then
    echo 'PEERS_TOTAL=0'
    return
  fi
  count=$(wc -l < "$file" | tr -d ' ')
  echo "PEERS_TOTAL=$count"
  while IFS='|' read -r address handshake endpoint server_rx server_tx; do
    echo "PEER_$address=HANDSHAKE_AGE_SECONDS=$handshake|ENDPOINT=$endpoint|SERVER_RX_BYTES=$server_rx|SERVER_TX_BYTES=$server_tx"
  done < "$file"
}

print_deltas() {
  before=$1
  after=$2
  if [ ! -s "$after" ]; then
    echo 'PEER_DELTA=none'
    return
  fi
  awk -F '|' '
    NR == FNR { previous[$1] = $0; next }
    {
      if (!($1 in previous)) {
        print "PEER_" $1 "_DELTA=NEW_PEER=yes|HANDSHAKE_CHANGED=yes|SERVER_RX_DELTA=" $4 "|SERVER_TX_DELTA=" $5
        next
      }
      split(previous[$1], old, "\\|")
      rx_delta = $4 - old[4]
      tx_delta = $5 - old[5]
      if (rx_delta < 0) rx_delta = $4
      if (tx_delta < 0) tx_delta = $5
      handshake_changed = ($2 != old[2]) ? "yes" : "no"
      print "PEER_" $1 "_DELTA=HANDSHAKE_CHANGED=" handshake_changed "|SERVER_RX_DELTA=" rx_delta "|SERVER_TX_DELTA=" tx_delta
    }
  ' "$before" "$after"
}

echo 'RESULT=success'
echo 'WIREGUARD_TRANSPORT_DIAGNOSTIC=read_only'
if "$DOCKER_BIN" port "$WIREGUARD_CONTAINER" 51820/udp 2>/dev/null | grep -Eq ':[0-9]+$'; then
  echo 'UDP_51820=published'
else
  echo 'UDP_51820=not_published'
fi
forwarding=$("$DOCKER_BIN" exec "$WIREGUARD_CONTAINER" sh -c 'cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || true' 2>/dev/null | tr -d '\r\n')
case "$forwarding" in 1) echo 'IPV4_FORWARD=enabled' ;; *) echo 'IPV4_FORWARD=unknown_or_disabled' ;; esac

echo 'BASELINE=begin'
snapshot "$TMP/before"
print_snapshot "$TMP/before"
echo 'BASELINE=end'
echo "WATCH_READY=toggle one affected WireGuard client now; seconds=$WATCH_SECONDS"
sleep "$WATCH_SECONDS"
echo 'OBSERVATION=begin'
snapshot "$TMP/after"
print_deltas "$TMP/before" "$TMP/after"
echo 'OBSERVATION=end'
