#!/bin/sh
# Read-only route evidence for Ozon, Avito and Yandex services.  It reports
# active policy rows, rendered provider placement and live Mihomo connections;
# it never changes rules, exits, credentials or client configuration.
set -eu

DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
GATEWAY_DIR=${GATEWAY_DIR:-/volume1/docker/vpn-gateway}
CONFIG=${CONFIG:-$GATEWAY_DIR/mihomo/config.yaml}
RULES_DIR=${RULES_DIR:-$GATEWAY_DIR/mihomo/rules}
DASHBOARD_CONTAINER=${DASHBOARD_CONTAINER:-vpn-dashboard}
WATCH_SECONDS=${1:-0}
SOURCE_ADDRESS=${2:-}
SELECTORS='ozon avito yandex category-ecommerce-ru category-retail-ru'

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }
[ -f "$CONFIG" ] || { echo "Mihomo config is missing: $CONFIG" >&2; exit 1; }
case "$WATCH_SECONDS" in
  ''|*[!0-9]*) echo 'watch duration must be an integer number of seconds' >&2; exit 1 ;;
esac
[ "$WATCH_SECONDS" -le 60 ] || { echo 'watch duration must not exceed 60 seconds' >&2; exit 1; }
case "$SOURCE_ADDRESS" in
  ''|*[!0-9.]*|.*|*.) echo 'source address must be an IPv4 address when set' >&2; exit 1 ;;
esac

echo 'RESULT=success'
echo 'RUSSIAN_SERVICES_DIAGNOSTIC=read_only'

echo 'POLICY_ROWS=begin'
"$DOCKER_BIN" exec -i "$DASHBOARD_CONTAINER" python - <<'PY'
import sqlite3

selectors = ('ozon', 'avito', 'yandex', 'category-ecommerce-ru', 'category-retail-ru')
connection = sqlite3.connect('/data/dashboard.sqlite3')
placeholders = ','.join('?' for _ in selectors)
rows = connection.execute(
    f'''
    SELECT kind, category, action, enabled
    FROM managed_rule_policies
    WHERE kind = 'GEOSITE' AND category IN ({placeholders})
    ORDER BY category, id
    ''',
    selectors,
).fetchall()
if not rows:
    print('POLICY_ROWS=none')
for kind, category, action, enabled in rows:
    print(f'POLICY={kind},{category}|ACTION={action}|ENABLED={int(bool(enabled))}')
PY
echo 'POLICY_ROWS=end'

echo 'PROVIDER_ASSIGNMENTS=begin'
for selector in $SELECTORS; do
  assignments=''
  for mapping in \
    'DIRECT:managed-direct.txt' \
    'WG-IMP:managed-wg-imp.txt' \
    'HY2-USA:managed-hy2-usa.txt' \
    'VPS-FALLBACK:managed-fallback.txt'; do
    action=${mapping%%:*}
    filename=${mapping#*:}
    file="$RULES_DIR/$filename"
    if [ -f "$file" ] && grep -Fx "GEOSITE,$selector" "$file" >/dev/null 2>&1; then
      if [ -n "$assignments" ]; then
        assignments="$assignments,$action"
      else
        assignments=$action
      fi
    fi
  done
  [ -n "$assignments" ] || assignments=none
  printf 'SELECTOR=%s|ASSIGNMENTS=%s\n' "$selector" "$assignments"
done
echo 'PROVIDER_ASSIGNMENTS=end'

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
"$DOCKER_BIN" exec -e "RUSSIA_SERVICES_WATCH_SECONDS=$WATCH_SECONDS" -e "RUSSIA_SERVICES_SOURCE_ADDRESS=$SOURCE_ADDRESS" -i "$DASHBOARD_CONTAINER" python - <<'PY'
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SERVICE_MARKERS = {
    'Ozon': ('ozon.ru', 'ozon.com', 'ozon.kz', 'ozon.by', 'ozonusercontent.com', 'o3.ru', 'o3t.ru', 'ocourier.ru'),
    'Avito': ('avito.ru', 'avito.st'),
    'Yandex': ('yandex', 'ya.ru', 'ya.cc', 'yandexgo.com', 'yango.com', 'yango.taxi', 'yastatic.net', 'yccdn.ru', 'yndx.net', 'clstorage.net'),
}

url = os.environ.get('MIHOMO_URL', 'http://vpn-wireguard:9091').rstrip('/') + '/connections'
headers = {}
secret_file = os.environ.get('MIHOMO_API_SECRET_FILE')
if secret_file:
    secret = Path(secret_file).read_text(encoding='utf-8').strip()
    if secret:
        headers['Authorization'] = f'Bearer {secret}'


def connections():
    request = Request(url, headers=headers, method='GET')
    with urlopen(request, timeout=5) as response:
        payload = json.load(response)
    items = payload.get('connections', [])
    return items if isinstance(items, list) else []


def describe(item):
    metadata = item.get('metadata') if isinstance(item, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    source = str(metadata.get('sourceIP', '')).strip()[:64] or 'unknown'
    host = str(metadata.get('host', '')).strip().lower().strip('.')[:253] or 'unknown'
    destination = str(metadata.get('destinationIP', '')).strip()[:64] or 'unknown'
    port = str(metadata.get('destinationPort', '')).strip()[:16] or 'unknown'
    network = str(metadata.get('network', '')).strip().lower()[:16] or 'unknown'
    rule = str(item.get('rule', 'unknown'))[:160]
    chains = item.get('chains', item.get('chain', []))
    chain = ','.join(str(value)[:80] for value in chains)[:253] if isinstance(chains, list) else ''
    return source, host, destination, port, network, rule, chain


def service_for(host):
    if host == 'unknown':
        return None
    for service, markers in SERVICE_MARKERS.items():
        for marker in markers:
            if host == marker or host.endswith('.' + marker):
                return service
    return None


def emit(prefix, index, item):
    source, host, destination, port, network, rule, chain = describe(item)
    service = service_for(host)
    if service is None:
        return None
    print(f'{prefix}_{index}=SOURCE={source}|SERVICE={service}|HOST={host}|DESTINATION={destination}:{port}|NETWORK={network}|RULE={rule}|CHAIN={chain}')
    return (source, service, host, destination, port, network, rule, chain)


try:
    initial = connections()
except (OSError, HTTPError, URLError, ValueError) as error:
    print(f'LIVE_CONNECTIONS_ERROR={type(error).__name__}')
else:
    source_filter = os.environ.get('RUSSIA_SERVICES_SOURCE_ADDRESS', '').strip()
    known = set()
    matched = 0
    for item in initial:
        source, host, _destination, _port, _network, _rule, _chain = describe(item)
        if source_filter and source != source_filter:
            continue
        service = service_for(host)
        if service is None:
            continue
        matched += 1
        key = emit('LIVE', matched, item)
        if key:
            known.add(key)
    print(f'LIVE_CONNECTIONS_MATCHED={matched}')

    watch_seconds = int(os.environ.get('RUSSIA_SERVICES_WATCH_SECONDS', '0'))
    if watch_seconds:
        baseline = {str(item.get('id', '')) for item in initial if isinstance(item, dict)}
        observed = set()
        print(f'WATCH_READY=open Ozon, Avito and a Yandex delivery page now; seconds={watch_seconds}')
        deadline = time.monotonic() + watch_seconds
        while time.monotonic() < deadline and len(observed) < 80:
            time.sleep(1)
            try:
                current = connections()
            except (OSError, HTTPError, URLError, ValueError):
                continue
            for item in current:
                if not isinstance(item, dict) or str(item.get('id', '')) in baseline:
                    continue
                source, host, _destination, _port, _network, _rule, _chain = describe(item)
                if source_filter and source != source_filter:
                    continue
                if service_for(host) is None:
                    continue
                key = (source, *describe(item)[1:])
                if key in observed or key in known:
                    continue
                observed.add(key)
                emit('WATCH', len(observed), item)
        print(f'WATCH_NEW_CONNECTIONS={len(observed)}')
PY
echo 'LIVE_CONNECTIONS=end'
