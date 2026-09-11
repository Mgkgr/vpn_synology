#!/bin/sh
# Read-only evidence for a Kinopoisk route issue.  It exposes neither Mihomo
# secrets nor client addresses and deletes its uploaded copy after execution.
set -eu

DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
GATEWAY_DIR=${GATEWAY_DIR:-/volume1/docker/vpn-gateway}
CONFIG=${CONFIG:-$GATEWAY_DIR/mihomo/config.yaml}
RULES_DIR=${RULES_DIR:-$GATEWAY_DIR/mihomo/rules}
DASHBOARD_CONTAINER=${DASHBOARD_CONTAINER:-vpn-dashboard}
WATCH_SECONDS=${1:-0}
SOURCE_ADDRESS=${2:-}

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }
[ -f "$CONFIG" ] || { echo "Mihomo config is missing: $CONFIG" >&2; exit 1; }
case "$WATCH_SECONDS" in
  ''|*[!0-9]*) echo 'watch duration must be an integer number of seconds' >&2; exit 1 ;;
esac
[ "$WATCH_SECONDS" -le 60 ] || { echo 'watch duration must not exceed 60 seconds' >&2; exit 1; }
is_ipv4() {
  printf '%s\n' "$1" | awk -F. '
    NF != 4 { exit 1 }
    {
      for (part = 1; part <= 4; part++) {
        if ($part !~ /^[0-9]+$/ || $part ~ /^0[0-9]+$/ || ($part + 0) > 255) exit 1
      }
    }
  '
}
if [ -n "$SOURCE_ADDRESS" ] && ! is_ipv4 "$SOURCE_ADDRESS"; then
  echo 'source address must be an IPv4 address when set' >&2
  exit 1
fi

echo 'RESULT=success'
echo 'KINOPOISK_DIAGNOSTIC=read_only'

echo 'POLICY_ROWS=begin'
"$DOCKER_BIN" exec -i "$DASHBOARD_CONTAINER" python - <<'PY'
import sqlite3

connection = sqlite3.connect("/data/dashboard.sqlite3")
rows = connection.execute(
    """
    SELECT kind, category, action, enabled
    FROM managed_rule_policies
    WHERE kind = 'GEOSITE' AND category IN ('kinopoisk', 'yandex', 'category-entertainment-ru')
    ORDER BY id
    """
).fetchall()
if not rows:
    print("POLICY_ROWS=none")
for kind, category, action, enabled in rows:
    print(f"POLICY={kind},{category}|ACTION={action}|ENABLED={int(bool(enabled))}")
PY
echo 'POLICY_ROWS=end'

echo 'MANAGED_DIRECT=begin'
if [ -f "$RULES_DIR/managed-direct.txt" ]; then
  total=$(wc -l < "$RULES_DIR/managed-direct.txt" | tr -d ' ')
  printf 'MANAGED_DIRECT_LINES=%s\n' "$total"
  for selector in kinopoisk yandex category-entertainment-ru; do
    if grep -Fx "GEOSITE,$selector" "$RULES_DIR/managed-direct.txt" >/dev/null 2>&1; then
      printf 'MANAGED_DIRECT_%s=yes\n' "$selector"
    else
      printf 'MANAGED_DIRECT_%s=no\n' "$selector"
    fi
  done
else
  echo 'MANAGED_DIRECT_FILE=missing'
fi
echo 'MANAGED_DIRECT=end'

echo 'RULE_PRIORITY=begin'
awk '
  /^rules:/ { in_rules = 1; next }
  !in_rules { next }
  /^  - / {
    payload = $0
    sub(/^  - /, "", payload)
    if (payload ~ /^RULE-SET,(direct-custom|managed-direct|managed-wg-imp|managed-hy2-usa|managed-fallback),/ ||
        payload ~ /^GEOSITE,/ || payload ~ /^GEOIP,/ || payload ~ /^MATCH/) {
      printf "RULE_LINE_%d=%s\n", NR, payload
    }
  }
' "$CONFIG"
echo 'RULE_PRIORITY=end'

echo 'MIHOMO_CONFIG_TEST=begin'
if "$DOCKER_BIN" exec "$DASHBOARD_CONTAINER" sh -c 'test -r /geodata/GeoSite.dat && test -r /geodata/GeoIP.dat'; then
  echo 'DASHBOARD_GEODATA=readable'
else
  echo 'DASHBOARD_GEODATA=unavailable'
fi
if "$DOCKER_BIN" exec vpn-mihomo /mihomo -t -d /root/.config/mihomo >/dev/null 2>&1; then
  echo 'MIHOMO_CONFIG=valid'
else
  echo 'MIHOMO_CONFIG=invalid'
fi
echo 'MIHOMO_CONFIG_TEST=end'

echo 'LIVE_CONNECTIONS=begin'
"$DOCKER_BIN" exec -e "KINOPOISK_WATCH_SECONDS=$WATCH_SECONDS" -e "KINOPOISK_SOURCE_ADDRESS=$SOURCE_ADDRESS" -i "$DASHBOARD_CONTAINER" python - <<'PY'
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

url = os.environ.get("MIHOMO_URL", "http://vpn-wireguard:9091").rstrip("/") + "/connections"
headers = {}
secret_file = os.environ.get("MIHOMO_API_SECRET_FILE")
if secret_file:
    secret = Path(secret_file).read_text(encoding="utf-8").strip()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"


def connections():
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=5) as response:
        payload = json.load(response)
    items = payload.get("connections", [])
    return items if isinstance(items, list) else []


def describe(item):
    metadata = item.get("metadata") if isinstance(item, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    source = str(metadata.get("sourceIP", "")).strip()[:64] or "unknown"
    host = str(metadata.get("host", "")).strip().lower()[:253] or "unknown"
    destination = str(metadata.get("destinationIP", "")).strip()[:64] or "unknown"
    port = str(metadata.get("destinationPort", "")).strip()[:16] or "unknown"
    network = str(metadata.get("network", "")).strip().lower()[:16] or "unknown"
    rule = str(item.get("rule", "unknown"))[:160]
    chains = item.get("chains", item.get("chain", []))
    if not isinstance(chains, list):
        chains = []
    chain = ",".join(str(value)[:80] for value in chains)[:253]
    return source, host, destination, port, network, rule, chain


try:
    initial = connections()
except (OSError, HTTPError, URLError, ValueError) as error:
    print(f"LIVE_CONNECTIONS_ERROR={type(error).__name__}")
else:
    source_filter = os.environ.get("KINOPOISK_SOURCE_ADDRESS", "").strip()
    matches = []
    for item in initial:
        source, host, _destination, _port, _network, rule, chain = describe(item)
        if source_filter and source != source_filter:
            continue
        if any(marker in host for marker in ("kinopoisk", "yandex", "yastatic", "yccdn", "clstorage")):
            matches.append((source, host, rule, chain))
    print(f"LIVE_CONNECTIONS_MATCHED={len(matches)}")
    for index, (source, host, rule, chain) in enumerate(matches, start=1):
        print(f"LIVE_{index}=SOURCE={source}|HOST={host}|RULE={rule}|CHAIN={chain}")

    watch_seconds = int(os.environ.get("KINOPOISK_WATCH_SECONDS", "0"))
    if watch_seconds:
        baseline = {str(item.get("id", "")) for item in initial if isinstance(item, dict)}
        observed = set()
        print(f"KINOPOISK_WATCH_READY=open or retry the TV app now; seconds={watch_seconds}")
        deadline = time.monotonic() + watch_seconds
        while time.monotonic() < deadline and len(observed) < 80:
            time.sleep(1)
            try:
                current = connections()
            except (OSError, HTTPError, URLError, ValueError):
                continue
            for item in current:
                if not isinstance(item, dict) or str(item.get("id", "")) in baseline:
                    continue
                source, host, destination, port, network, rule, chain = describe(item)
                if source_filter and source != source_filter:
                    continue
                key = (source, host, destination, port, network, rule, chain)
                if key in observed:
                    continue
                observed.add(key)
                print(f"TV_WATCH_{len(observed)}=SOURCE={source}|HOST={host}|DESTINATION={destination}:{port}|NETWORK={network}|RULE={rule}|CHAIN={chain}")
        print(f"TV_WATCH_NEW_CONNECTIONS={len(observed)}")
PY
echo 'LIVE_CONNECTIONS=end'
