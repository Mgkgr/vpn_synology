#!/bin/sh
# Read-only evidence for access from WireGuard clients to 192.168.3.0/24.
# It does not reload Mihomo, alter routes, or disclose gateway secrets.
set -eu

DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
GATEWAY_DIR=${GATEWAY_DIR:-/volume1/docker/vpn-gateway}
CONFIG=${CONFIG:-$GATEWAY_DIR/mihomo/config.yaml}
DIRECT_RULES=${DIRECT_RULES:-$GATEWAY_DIR/mihomo/rules/direct.txt}
WIREGUARD_CONTAINER=${WIREGUARD_CONTAINER:-vpn-wireguard}
MIHOMO_CONTAINER=${MIHOMO_CONTAINER:-vpn-mihomo}
DASHBOARD_CONTAINER=${DASHBOARD_CONTAINER:-vpn-dashboard}
WATCH_SECONDS=${1:-30}
SOURCE_ADDRESS=${2:-}
LAN_CIDR=192.168.3.0/24
LAN_PROBE=192.168.3.1

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }
[ -f "$CONFIG" ] || { echo "Mihomo config is missing: $CONFIG" >&2; exit 1; }
[ -f "$DIRECT_RULES" ] || { echo "DIRECT rules are missing: $DIRECT_RULES" >&2; exit 1; }
case "$WATCH_SECONDS" in ''|*[!0-9]*) echo 'watch duration must be an integer number of seconds' >&2; exit 1 ;; esac
[ "$WATCH_SECONDS" -le 60 ] || { echo 'watch duration must not exceed 60 seconds' >&2; exit 1; }
case "$SOURCE_ADDRESS" in ''|*[!0-9.]*|.*|*.) echo 'source address must be an IPv4 address when set' >&2; exit 1 ;; esac

for container in "$WIREGUARD_CONTAINER" "$MIHOMO_CONTAINER" "$DASHBOARD_CONTAINER"; do
  "$DOCKER_BIN" inspect "$container" >/dev/null 2>&1 || { echo "required container is missing: $container" >&2; exit 1; }
done

echo 'RESULT=success'
echo 'LAN3_DIAGNOSTIC=read_only'

echo 'CONTAINERS=begin'
"$DOCKER_BIN" inspect -f 'CONTAINER={{.Name}}|STATE={{.State.Status}}|RUNNING={{.State.Running}}|RESTARTS={{.RestartCount}}|NETWORK={{.HostConfig.NetworkMode}}' "$WIREGUARD_CONTAINER" "$MIHOMO_CONTAINER"
echo 'CONTAINERS=end'

echo 'MIHOMO_EXCEPTIONS=begin'
for cidr in 192.168.2.0/24 "$LAN_CIDR" 10.66.0.0/24; do
  if grep -Fqx "    - $cidr" "$CONFIG"; then
    echo "TUN_EXCLUDE_$cidr=yes"
  else
    echo "TUN_EXCLUDE_$cidr=no"
  fi
done
if grep -Fqx "IP-CIDR,$LAN_CIDR,DIRECT,no-resolve" "$DIRECT_RULES" || grep -Fqx "IP-CIDR,$LAN_CIDR" "$DIRECT_RULES"; then
  echo 'LAN3_DIRECT_RULE=yes'
else
  echo 'LAN3_DIRECT_RULE=no'
fi
echo 'MIHOMO_EXCEPTIONS=end'

echo 'HOST_ROUTE=begin'
if [ -x /sbin/ip ]; then
  /sbin/ip route get "$LAN_PROBE" 2>&1 | sed 's/^/HOST_ROUTE=/'
elif command -v ip >/dev/null 2>&1; then
  ip route get "$LAN_PROBE" 2>&1 | sed 's/^/HOST_ROUTE=/'
else
  echo 'HOST_ROUTE=ip_command_unavailable'
fi
echo 'HOST_ROUTE=end'

echo 'WIREGUARD_NAMESPACE_ROUTE=begin'
"$DOCKER_BIN" exec "$WIREGUARD_CONTAINER" sh -c '
  if command -v ip >/dev/null 2>&1; then
    ip route get 192.168.3.1 2>&1
  else
    echo ip_command_unavailable
  fi
' 2>&1 | sed 's/^/WG_ROUTE=/' || true
echo 'WIREGUARD_NAMESPACE_ROUTE=end'

echo 'MIHOMO_CONFIG=begin'
if "$DOCKER_BIN" exec "$MIHOMO_CONTAINER" /mihomo -t -d /root/.config/mihomo >/dev/null 2>&1; then
  echo 'MIHOMO_CONFIG=valid'
else
  echo 'MIHOMO_CONFIG=invalid_or_container_unavailable'
fi
echo 'MIHOMO_CONFIG=end'

echo 'LAN3_CONNECTION_WATCH=begin'
"$DOCKER_BIN" exec -e "LAN3_WATCH_SECONDS=$WATCH_SECONDS" -e "LAN3_SOURCE_ADDRESS=$SOURCE_ADDRESS" -i "$DASHBOARD_CONTAINER" python - <<'PY'
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

base_url = os.environ.get("MIHOMO_URL", "http://vpn-wireguard:9091").rstrip("/")
headers = {}
secret_file = os.environ.get("MIHOMO_API_SECRET_FILE")
if secret_file:
    secret = Path(secret_file).read_text(encoding="utf-8").strip()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"


def connections():
    with urlopen(Request(base_url + "/connections", headers=headers), timeout=5) as response:
        payload = json.load(response)
    items = payload.get("connections", [])
    return items if isinstance(items, list) else []


def describe(item):
    metadata = item.get("metadata") if isinstance(item, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    source = str(metadata.get("sourceIP", "")).strip()[:64] or "unknown"
    destination = str(metadata.get("destinationIP", "")).strip()[:64] or "unknown"
    port = str(metadata.get("destinationPort", "")).strip()[:16] or "unknown"
    network = str(metadata.get("network", "")).strip()[:16] or "unknown"
    rule = str(item.get("rule", "unknown"))[:120]
    chains = item.get("chains", item.get("chain", []))
    chain = ",".join(str(value)[:80] for value in chains)[:253] if isinstance(chains, list) else ""
    return source, destination, port, network, rule, chain


try:
    initial = connections()
except (OSError, HTTPError, URLError, ValueError) as error:
    print(f"LAN3_CONTROLLER_ERROR={type(error).__name__}")
else:
    source_filter = os.environ.get("LAN3_SOURCE_ADDRESS", "").strip()
    baseline = {str(item.get("id", "")) for item in initial if isinstance(item, dict)}
    seen = set()
    print(f"LAN3_WATCH_READY=open http://192.168.3.1 from the WireGuard client now; seconds={os.environ.get('LAN3_WATCH_SECONDS', '0')}")
    deadline = time.monotonic() + int(os.environ.get("LAN3_WATCH_SECONDS", "0"))
    while time.monotonic() < deadline and len(seen) < 30:
        time.sleep(1)
        try:
            current = connections()
        except (OSError, HTTPError, URLError, ValueError):
            continue
        for item in current:
            if not isinstance(item, dict) or str(item.get("id", "")) in baseline:
                continue
            source, destination, port, network, rule, chain = describe(item)
            if source_filter and source != source_filter:
                continue
            if not destination.startswith("192.168.3."):
                continue
            key = (source, destination, port, network, rule, chain)
            if key in seen:
                continue
            seen.add(key)
            print(f"LAN3_CONNECTION_{len(seen)}=SOURCE={source}|DESTINATION={destination}:{port}|NETWORK={network}|RULE={rule}|CHAIN={chain}")
    print(f"LAN3_NEW_CONNECTIONS={len(seen)}")
PY
echo 'LAN3_CONNECTION_WATCH=end'
