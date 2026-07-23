#!/bin/sh
# Read-only evidence for a Kinopoisk route issue.  It exposes neither Mihomo
# secrets nor client addresses and deletes its uploaded copy after execution.
set -eu

DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
GATEWAY_DIR=${GATEWAY_DIR:-/volume1/docker/vpn-gateway}
CONFIG=${CONFIG:-$GATEWAY_DIR/mihomo/config.yaml}
RULES_DIR=${RULES_DIR:-$GATEWAY_DIR/mihomo/rules}
DASHBOARD_CONTAINER=${DASHBOARD_CONTAINER:-vpn-dashboard}

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }
[ -f "$CONFIG" ] || { echo "Mihomo config is missing: $CONFIG" >&2; exit 1; }

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
    if (payload ~ /^RULE-SET,(direct-custom|managed-direct|managed-wg-imp|managed-hy2-nl|managed-fallback),/ ||
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
"$DOCKER_BIN" exec -i "$DASHBOARD_CONTAINER" python - <<'PY'
import json
import os
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
try:
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=5) as response:
        payload = json.load(response)
except (OSError, HTTPError, URLError, ValueError) as error:
    print(f"LIVE_CONNECTIONS_ERROR={type(error).__name__}")
else:
    matches = []
    for item in payload.get("connections", []):
        metadata = item.get("metadata") if isinstance(item, dict) else None
        metadata = metadata if isinstance(metadata, dict) else {}
        host = str(metadata.get("host", "")).strip().lower()
        if any(marker in host for marker in ("kinopoisk", "yandex", "yastatic", "yccdn", "clstorage")):
            rule = str(item.get("rule", "unknown"))[:160]
            chains = item.get("chains", item.get("chain", []))
            if not isinstance(chains, list):
                chains = []
            chain = ",".join(str(value)[:80] for value in chains)
            matches.append((host[:253], rule, chain[:253]))
    print(f"LIVE_CONNECTIONS_MATCHED={len(matches)}")
    for index, (host, rule, chain) in enumerate(matches, start=1):
        print(f"LIVE_{index}=HOST={host}|RULE={rule}|CHAIN={chain}")
PY
echo 'LIVE_CONNECTIONS=end'
