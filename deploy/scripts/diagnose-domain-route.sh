#!/bin/sh
# Read-only route evidence for a single hostname. It never changes Mihomo,
# rules, exit profiles, client profiles, or secrets.
set -eu

DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
GATEWAY_DIR=${GATEWAY_DIR:-/volume1/docker/vpn-gateway}
CONFIG=${CONFIG:-$GATEWAY_DIR/mihomo/config.yaml}
RULES_DIR=${RULES_DIR:-$GATEWAY_DIR/mihomo/rules}
DASHBOARD_CONTAINER=${DASHBOARD_CONTAINER:-vpn-dashboard}
DOMAIN=${1:-}
WATCH_SECONDS=${2:-45}
SOURCE_ADDRESS=${3:-}

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }
[ -f "$CONFIG" ] || { echo "Mihomo config is missing: $CONFIG" >&2; exit 1; }
case "$DOMAIN" in
  ''|*[!A-Za-z0-9.-]*|.*|*.) echo 'domain must be a DNS hostname' >&2; exit 1 ;;
esac
case "$WATCH_SECONDS" in
  ''|*[!0-9]*) echo 'watch duration must be an integer number of seconds' >&2; exit 1 ;;
esac
[ "$WATCH_SECONDS" -le 60 ] || { echo 'watch duration must not exceed 60 seconds' >&2; exit 1; }
case "$SOURCE_ADDRESS" in
  ''|*[!0-9.]*|.*|*.) echo 'source address must be an IPv4 address when set' >&2; exit 1 ;;
esac

echo 'RESULT=success'
echo 'DOMAIN_ROUTE_DIAGNOSTIC=read_only'
printf 'DOMAIN=%s\n' "$DOMAIN"

echo 'RULE_FILES=begin'
for filename in direct.txt managed-direct.txt managed-wg-imp.txt managed-hy2-nl.txt managed-hy2-usa.txt managed-fallback.txt; do
  file="$RULES_DIR/$filename"
  if [ -f "$file" ]; then
    if grep -E -i -x "DOMAIN(-SUFFIX|-KEYWORD)?,$DOMAIN(,DIRECT|,WG-IMP|,HY2-(NL|USA)|,VPS-FALLBACK)?" "$file" >/dev/null 2>&1 || grep -E -i -x "DOMAIN-SUFFIX,${DOMAIN#*.}(,DIRECT|,WG-IMP|,HY2-(NL|USA)|,VPS-FALLBACK)?" "$file" >/dev/null 2>&1; then
      printf 'FILE=%s|MATCH=yes\n' "$filename"
    else
      printf 'FILE=%s|MATCH=no\n' "$filename"
    fi
  else
    printf 'FILE=%s|STATE=missing\n' "$filename"
  fi
done
echo 'RULE_FILES=end'

echo 'CONFIG_ANCHORS=begin'
for anchor in \
  'RULE-SET,direct-custom,DIRECT' \
  'RULE-SET,managed-direct,DIRECT' \
  'RULE-SET,managed-wg-imp,WG-IMP' \
  'RULE-SET,managed-hy2-nl,HY2-NL' \
  'RULE-SET,managed-hy2-usa,HY2-USA' \
  'RULE-SET,managed-fallback,VPS-FALLBACK' \
  'MATCH,VPS-FALLBACK'; do
  if grep -F -x "  - $anchor" "$CONFIG" >/dev/null 2>&1; then
    printf 'ANCHOR=%s|PRESENT=yes\n' "$anchor"
  else
    printf 'ANCHOR=%s|PRESENT=no\n' "$anchor"
  fi
done
echo 'CONFIG_ANCHORS=end'

echo 'MIHOMO_CONFIG_TEST=begin'
if "$DOCKER_BIN" exec vpn-mihomo /mihomo -t -d /root/.config/mihomo >/dev/null 2>&1; then
  echo 'MIHOMO_CONFIG=valid'
else
  echo 'MIHOMO_CONFIG=invalid'
fi
echo 'MIHOMO_CONFIG_TEST=end'

echo 'LIVE_CONNECTIONS=begin'
"$DOCKER_BIN" exec \
  -e "DOMAIN_ROUTE_DOMAIN=$DOMAIN" \
  -e "DOMAIN_ROUTE_WATCH_SECONDS=$WATCH_SECONDS" \
  -e "DOMAIN_ROUTE_SOURCE_ADDRESS=$SOURCE_ADDRESS" \
  -i "$DASHBOARD_CONTAINER" python - <<'PY'
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

domain = os.environ['DOMAIN_ROUTE_DOMAIN'].lower().rstrip('.')
source_filter = os.environ.get('DOMAIN_ROUTE_SOURCE_ADDRESS', '').strip()
watch_seconds = int(os.environ['DOMAIN_ROUTE_WATCH_SECONDS'])
base = os.environ.get('MIHOMO_URL', 'http://vpn-wireguard:9091').rstrip('/')
headers = {}
secret_file = os.environ.get('MIHOMO_API_SECRET_FILE')
if secret_file:
    secret = Path(secret_file).read_text(encoding='utf-8').strip()
    if secret:
        headers['Authorization'] = f'Bearer {secret}'


def connections():
    request = Request(base + '/connections', headers=headers, method='GET')
    with urlopen(request, timeout=5) as response:
        payload = json.load(response)
    items = payload.get('connections', [])
    return items if isinstance(items, list) else []


def describe(item):
    metadata = item.get('metadata') if isinstance(item, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    source = str(metadata.get('sourceIP', '')).strip()[:64] or 'unknown'
    host = str(metadata.get('host', '')).strip().lower().rstrip('.')[:253] or 'unknown'
    destination = str(metadata.get('destinationIP', '')).strip()[:64] or 'unknown'
    port = str(metadata.get('destinationPort', '')).strip()[:16] or 'unknown'
    network = str(metadata.get('network', '')).strip().lower()[:16] or 'unknown'
    rule = str(item.get('rule', 'unknown'))[:160]
    chains = item.get('chains', item.get('chain', []))
    chain = ','.join(str(value)[:80] for value in chains)[:253] if isinstance(chains, list) else ''
    return source, host, destination, port, network, rule, chain


def matching(item):
    source, host, *_ = describe(item)
    return (not source_filter or source == source_filter) and (host == domain or host.endswith('.' + domain))


def emit(prefix, index, item):
    source, host, destination, port, network, rule, chain = describe(item)
    print(f'{prefix}_{index}=SOURCE={source}|HOST={host}|DESTINATION={destination}:{port}|NETWORK={network}|RULE={rule}|CHAIN={chain}')


try:
    initial = connections()
except (OSError, HTTPError, URLError, ValueError) as error:
    print(f'LIVE_CONNECTIONS_ERROR={type(error).__name__}')
else:
    existing = [item for item in initial if matching(item)]
    print(f'LIVE_CONNECTIONS_MATCHED={len(existing)}')
    for index, item in enumerate(existing, start=1):
        emit('LIVE', index, item)
    baseline = {str(item.get('id', '')) for item in initial if isinstance(item, dict)}
    observed = set()
    print(f'WATCH_READY=open or reload the exact domain now; seconds={watch_seconds}')
    deadline = time.monotonic() + watch_seconds
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            current = connections()
        except (OSError, HTTPError, URLError, ValueError):
            continue
        for item in current:
            if not isinstance(item, dict) or str(item.get('id', '')) in baseline or not matching(item):
                continue
            details = describe(item)
            if details in observed:
                continue
            observed.add(details)
            emit('WATCH', len(observed), item)
    print(f'WATCH_NEW_CONNECTIONS={len(observed)}')
PY
echo 'LIVE_CONNECTIONS=end'
