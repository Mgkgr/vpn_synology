#!/bin/sh
# Read-only evidence for an interrupted domain-sniffer activation.  It avoids
# restarts and never prints Mihomo credentials or the full gateway config.
set -eu

GATEWAY_DIR=${GATEWAY_DIR:-/volume1/docker/vpn-gateway}
CONFIG=${CONFIG:-$GATEWAY_DIR/mihomo/config.yaml}
DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
MIHOMO_CONTAINER=${MIHOMO_CONTAINER:-vpn-mihomo}
DASHBOARD_CONTAINER=${DASHBOARD_CONTAINER:-vpn-dashboard}

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }
[ -f "$CONFIG" ] || { echo "Mihomo config is missing: $CONFIG" >&2; exit 1; }
"$DOCKER_BIN" inspect "$MIHOMO_CONTAINER" >/dev/null 2>&1 || { echo 'Mihomo container is missing' >&2; exit 1; }
"$DOCKER_BIN" inspect "$DASHBOARD_CONTAINER" >/dev/null 2>&1 || { echo 'dashboard container is missing' >&2; exit 1; }

echo 'RESULT=success'
echo 'DOMAIN_SNIFFER_DIAGNOSTIC=read_only'

"$DOCKER_BIN" inspect -f 'MIHOMO_STATE={{.State.Status}}|RUNNING={{.State.Running}}|EXIT_CODE={{.State.ExitCode}}|RESTARTS={{.RestartCount}}|STARTED={{.State.StartedAt}}|FINISHED={{.State.FinishedAt}}|ERROR={{.State.Error}}' "$MIHOMO_CONTAINER"

echo 'RELATED_CONTAINERS=begin'
"$DOCKER_BIN" ps -a --format '{{.Names}}|{{.State}}|{{.Status}}' | awk '/^(vpn-mihomo|vpn-wireguard|vpn-dashboard)\|/ { print "CONTAINER=" $0 }'
echo 'RELATED_CONTAINERS=end'

echo 'SNIFFER_CONFIG=begin'
if grep -q '^sniffer:' "$CONFIG"; then
  echo 'SNIFFER_BLOCK=present'
else
  echo 'SNIFFER_BLOCK=absent'
fi
if grep -q '^ *parse-pure-ip: true$' "$CONFIG"; then
  echo 'PURE_IP_SNIFFING=enabled'
else
  echo 'PURE_IP_SNIFFING=absent'
fi
if grep -q '^ *force-dns-mapping: true$' "$CONFIG"; then
  echo 'FORCE_DNS_MAPPING=enabled'
else
  echo 'FORCE_DNS_MAPPING=absent'
fi
echo 'SNIFFER_CONFIG=end'

if "$DOCKER_BIN" exec "$MIHOMO_CONTAINER" /mihomo -t -d /root/.config/mihomo >/dev/null 2>&1; then
  echo 'MIHOMO_CONFIG=valid'
else
  echo 'MIHOMO_CONFIG=invalid'
fi

echo 'CONTROLLER=begin'
"$DOCKER_BIN" exec -i "$DASHBOARD_CONTAINER" python - <<'PY'
import os
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
try:
    with urlopen(Request(base_url + "/version", headers=headers), timeout=5) as response:
        print(f"CONTROLLER_HTTP={response.status}")
except HTTPError as error:
    print(f"CONTROLLER_HTTP={error.code}")
except (OSError, URLError, ValueError) as error:
    print(f"CONTROLLER_ERROR={type(error).__name__}")
PY
echo 'CONTROLLER=end'

echo 'MIHOMO_WARNINGS=begin'
"$DOCKER_BIN" logs --tail 120 "$MIHOMO_CONTAINER" 2>&1 | awk '
  tolower($0) ~ /(error|fatal|warn|panic|fail)/ {
    line = $0
    if (length(line) > 400) line = substr(line, 1, 400)
    print "MIHOMO_LOG=" line
  }
'
echo 'MIHOMO_WARNINGS=end'
