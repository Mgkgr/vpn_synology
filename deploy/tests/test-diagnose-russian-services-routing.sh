#!/bin/sh
# Regression: the Russian-service capture may run for every client, so an
# empty source filter is valid. A malformed IPv4 address must fail before the
# script can query the controller.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
SCRIPT="$ROOT/deploy/scripts/diagnose-russian-services-routing.sh"
TMP=$(mktemp -d)
server_pid=''
cleanup() {
  [ -z "$server_pid" ] || kill "$server_pid" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT
mkdir -p "$TMP/bin" "$TMP/gateway/mihomo/rules"

cat > "$TMP/bin/id" <<'EOF'
#!/bin/sh
printf '0\n'
EOF
chmod 700 "$TMP/bin/id"

cat > "$TMP/bin/docker" <<'EOF'
#!/bin/sh
# The diagnostic only needs successful read-only controller calls. For its
# connection consumer, execute the actual embedded Python against our local
# immutable controller fixture.
set -eu
[ "${1:-}" = 'exec' ] && shift
while [ "$#" -gt 0 ]; do
  case "$1" in
    -e) export "$2"; shift 2 ;;
    -i) shift ;;
    *) break ;;
  esac
done
container=${1:-}
[ "$#" -gt 0 ] && shift
command=${1:-}
[ "$#" -gt 0 ] && shift
if [ "$container" = 'vpn-dashboard' ] && [ "$command" = 'python' ] && [ "${1:-}" = '-' ]; then
  if [ -n "${RUSSIA_SERVICES_WATCH_SECONDS+x}" ]; then
    "$TEST_PYTHON" -
  else
    cat >/dev/null
  fi
fi
exit 0
EOF
chmod 700 "$TMP/bin/docker"

cat > "$TMP/gateway/mihomo/config.yaml" <<'EOF'
rules:
  - RULE-SET,direct-custom,DIRECT
  - MATCH,VPS-FALLBACK
EOF
touch "$TMP/gateway/mihomo/rules/direct.txt"

cat > "$TMP/controller.py" <<'EOF'
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer


PAYLOAD = {
    "connections": [
        {
            "id": "yandex-food",
            "metadata": {
                "sourceIP": "10.66.0.5",
                "host": "eda.yandex.ru",
                "destinationIP": "203.0.113.10",
                "destinationPort": "443",
                "network": "tcp",
            },
            "rule": "RuleSet",
            "chains": ["DIRECT"],
        },
        {
            "id": "samokat",
            "metadata": {
                "sourceIP": "10.66.0.5",
                "host": "api.samokat.ru",
                "destinationIP": "203.0.113.11",
                "destinationPort": "443",
                "network": "tcp",
            },
            "rule": "Match",
            "chains": ["WG-IMP", "VPS-FALLBACK"],
        },
        {
            "id": "wildberries",
            "metadata": {
                "sourceIP": "10.66.0.5",
                "host": "www.wildberries.ru",
                "destinationIP": "203.0.113.12",
                "destinationPort": "443",
                "network": "tcp",
            },
            "rule": "RuleSet",
            "chains": ["DIRECT"],
        },
    ]
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(PAYLOAD).encode("utf-8"))

    def log_message(self, _format: str, *_args: object) -> None:
        return


server = HTTPServer(("127.0.0.1", 0), Handler)
print(server.server_port, flush=True)
server.serve_forever()
EOF

TEST_PYTHON=${TEST_PYTHON:-python}
"$TEST_PYTHON" "$TMP/controller.py" > "$TMP/port" 2> "$TMP/server.err" &
server_pid=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -s "$TMP/port" ] && break
  sleep 1
done
[ -s "$TMP/port" ] || { cat "$TMP/server.err" >&2; exit 1; }
controller_port=$(cat "$TMP/port")

output=$(PATH="$TMP/bin:$PATH" \
  DOCKER_BIN="$TMP/bin/docker" \
  TEST_PYTHON="$TEST_PYTHON" \
  MIHOMO_URL="http://127.0.0.1:$controller_port" \
  GATEWAY_DIR="$TMP/gateway" \
  CONFIG="$TMP/gateway/mihomo/config.yaml" \
  RULES_DIR="$TMP/gateway/mihomo/rules" \
  /bin/sh "$SCRIPT" 0 '10.66.0.5')

printf '%s\n' "$output" | grep -Fqx 'RESULT=success'
printf '%s\n' "$output" | grep -Fqx 'RUSSIAN_SERVICES_DIAGNOSTIC=read_only'
printf '%s\n' "$output" | grep -Fqx 'LIVE_CONNECTIONS_MATCHED=3'
printf '%s\n' "$output" | grep -F 'SERVICE=Yandex-food|HOST=eda.yandex.ru|' >/dev/null
printf '%s\n' "$output" | grep -F 'SERVICE=Samokat|HOST=api.samokat.ru|' >/dev/null
printf '%s\n' "$output" | grep -F 'SERVICE=Wildberries|HOST=www.wildberries.ru|' >/dev/null

empty_filter_output=$(PATH="$TMP/bin:$PATH" \
  DOCKER_BIN="$TMP/bin/docker" \
  TEST_PYTHON="$TEST_PYTHON" \
  MIHOMO_URL="http://127.0.0.1:$controller_port" \
  GATEWAY_DIR="$TMP/gateway" \
  CONFIG="$TMP/gateway/mihomo/config.yaml" \
  RULES_DIR="$TMP/gateway/mihomo/rules" \
  /bin/sh "$SCRIPT" 0 '')
printf '%s\n' "$empty_filter_output" | grep -Fqx 'RESULT=success'

if PATH="$TMP/bin:$PATH" \
  DOCKER_BIN="$TMP/bin/docker" \
  GATEWAY_DIR="$TMP/gateway" \
  CONFIG="$TMP/gateway/mihomo/config.yaml" \
  RULES_DIR="$TMP/gateway/mihomo/rules" \
  /bin/sh "$SCRIPT" 0 '10.66.0.999' >/dev/null 2>&1; then
  echo 'an out-of-range source address was accepted' >&2
  exit 1
fi

printf '%s\n' 'PASS test-diagnose-russian-services-routing'
