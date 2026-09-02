#!/bin/sh
# Apply the single reserve Hysteria2 migration only after every reversible
# check succeeds. This script is designed to run detached: a Mihomo hot reload
# can intentionally drop the SSH tunnel that is itself travelling through it.
# The Python helper verifies the HY2-NL -> HY2-USA config references exactly.
# The authenticated controller proxy_delay request is the final live check.
set -eu

SOURCE_PROFILE=${1:?missing input profile}
RENAMER=${2:?missing rename-hy2-usa.py helper}
STATUS_FILE=${3:?missing public status file}
PRIVATE_LOG=${4:?missing private log file}
GATEWAY_DIR=${GATEWAY_DIR:-/volume1/docker/vpn-gateway}
CONFIG=${CONFIG:-$GATEWAY_DIR/mihomo/config.yaml}
RULES_DIR=${RULES_DIR:-$GATEWAY_DIR/mihomo/rules}
TARGET_PROFILE=${TARGET_PROFILE:-$GATEWAY_DIR/secrets/hysteria2.env}
BACKUPS_DIR=${BACKUPS_DIR:-$GATEWAY_DIR/backups}
DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
MIHOMO_CONTAINER=${MIHOMO_CONTAINER:-vpn-mihomo}
DASHBOARD_CONTAINER=${DASHBOARD_CONTAINER:-vpn-dashboard}
TEST_URL=${TEST_URL:-https://cp.cloudflare.com/generate_204}

safe_status() {
  umask 022
  temporary="${STATUS_FILE}.tmp.$$"
  printf '%s\n' "$@" > "$temporary"
  chmod 0644 "$temporary"
  mv -f "$temporary" "$STATUS_FILE"
}

cleanup() {
  rm -f "$SOURCE_PROFILE" "$RENAMER" "$PRIVATE_LOG" "${PRIVATE_LOG}.pid" "$0"
}
trap cleanup EXIT INT TERM

fail() {
  safe_status 'RESULT=failed' "REASON=$1"
  exit 1
}

[ "$(id -u)" = 0 ] || fail 'must run as root'
[ -x "$DOCKER_BIN" ] || fail 'Docker binary is unavailable'
[ -f "$SOURCE_PROFILE" ] || fail 'input profile is missing'
[ -f "$RENAMER" ] || fail 'migration helper is missing'
[ -x /usr/bin/python3 ] || fail 'python3 is unavailable'
[ -f "$CONFIG" ] || fail 'Mihomo configuration is missing'
[ -f "$TARGET_PROFILE" ] || fail 'existing reserve profile is missing'
[ -d "$RULES_DIR" ] || fail 'Mihomo rules directory is missing'
"$DOCKER_BIN" inspect "$MIHOMO_CONTAINER" >/dev/null 2>&1 || fail 'Mihomo container is missing'
"$DOCKER_BIN" inspect "$DASHBOARD_CONTAINER" >/dev/null 2>&1 || fail 'dashboard container is missing'

old_rule="$RULES_DIR/managed-hy2-nl.txt"
new_rule="$RULES_DIR/managed-hy2-usa.txt"
if [ -f "$old_rule" ] && [ ! -e "$new_rule" ]; then
  rule_name=managed-hy2-nl.txt
elif [ -f "$new_rule" ] && [ ! -e "$old_rule" ]; then
  rule_name=managed-hy2-usa.txt
else
  fail 'HY2 managed rule file state is ambiguous'
fi

umask 077
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="$BACKUPS_DIR/hy2-usa-before-$stamp"
mkdir -p "$backup_dir"
cp -p "$TARGET_PROFILE" "$backup_dir/hysteria2.env"
cp -p "$CONFIG" "$backup_dir/config.yaml"
cp -p "$RULES_DIR/$rule_name" "$backup_dir/$rule_name"
printf '%s\n' "$rule_name" > "$backup_dir/rule-name"
chmod 0600 "$backup_dir"/*

restore() {
  cp -p "$backup_dir/hysteria2.env" "$TARGET_PROFILE"
  cp -p "$backup_dir/config.yaml" "$CONFIG"
  rm -f "$old_rule" "$new_rule"
  saved_rule_name=$(cat "$backup_dir/rule-name")
  cp -p "$backup_dir/$saved_rule_name" "$RULES_DIR/$saved_rule_name"
  "$DOCKER_BIN" exec "$MIHOMO_CONTAINER" /mihomo -t -d /root/.config/mihomo >/dev/null 2>&1 || return 1
  "$DOCKER_BIN" exec -i "$DASHBOARD_CONTAINER" python - <<'PY' >/dev/null 2>&1 || return 1
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

base = os.environ.get("MIHOMO_URL", "http://vpn-wireguard:9091").rstrip("/")
headers = {"Content-Type": "application/json"}
secret_file = os.environ.get("MIHOMO_API_SECRET_FILE")
if secret_file:
    secret = Path(secret_file).read_text(encoding="utf-8").strip()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
request = Request(base + "/configs?force=true", data=b'{"path":"","payload":""}', headers=headers, method="PUT")
with urlopen(request, timeout=15) as response:
    if not 200 <= response.status < 300:
        raise RuntimeError("controller reload did not succeed")
PY
}

if ! "$DOCKER_BIN" exec "$MIHOMO_CONTAINER" /mihomo -t -d /root/.config/mihomo >/dev/null 2>&1; then
  fail 'current Mihomo configuration is invalid'
fi

profile_mode=$(stat -c '%a' "$TARGET_PROFILE")
profile_owner=$(stat -c '%u:%g' "$TARGET_PROFILE")
profile_tmp=$(mktemp "$GATEWAY_DIR/secrets/.hysteria2.next.XXXXXX")
cp "$SOURCE_PROFILE" "$profile_tmp"
chown "$profile_owner" "$profile_tmp"
chmod "$profile_mode" "$profile_tmp"
mv -f "$profile_tmp" "$TARGET_PROFILE"

if ! /usr/bin/python3 "$RENAMER" "$TARGET_PROFILE" "$CONFIG" "$RULES_DIR"; then
  restore || true
  fail 'HY2-USA migration validation failed; backup was restored'
fi
if ! "$DOCKER_BIN" exec "$MIHOMO_CONTAINER" /mihomo -t -d /root/.config/mihomo >/dev/null 2>&1; then
  restore || true
  fail 'Mihomo configuration validation failed; backup was restored'
fi

if ! delay_output=$("$DOCKER_BIN" exec -i -e "HY2_TEST_URL=$TEST_URL" "$DASHBOARD_CONTAINER" python - 2>&1 <<'PY'
import json
import os
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

base = os.environ.get("MIHOMO_URL", "http://vpn-wireguard:9091").rstrip("/")
headers = {"Content-Type": "application/json"}
secret_file = os.environ.get("MIHOMO_API_SECRET_FILE")
if secret_file:
    secret = Path(secret_file).read_text(encoding="utf-8").strip()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

reload_request = Request(base + "/configs?force=true", data=b'{"path":"","payload":""}', headers=headers, method="PUT")
with urlopen(reload_request, timeout=15) as response:
    if not 200 <= response.status < 300:
        raise RuntimeError("controller reload did not succeed")

last_error = None
for _attempt in range(12):
    try:
        endpoint = base + "/proxies/" + quote("HY2-USA", safe="") + "/delay?url=" + quote(os.environ["HY2_TEST_URL"], safe="") + "&timeout=10000"
        with urlopen(Request(endpoint, headers=headers), timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
            delay = payload.get("delay")
            if not isinstance(delay, int) or delay < 0:
                raise RuntimeError("delay response is invalid")
            print(f"HY2_DELAY_MS={delay}")
            break
    except Exception as error:
        last_error = error
        time.sleep(1)
else:
    raise RuntimeError("HY2-USA delay probe did not become ready")
PY
); then
  restore || true
  fail 'controller reload or HY2-USA delay test failed; backup was restored'
fi

case "$delay_output" in
  HY2_DELAY_MS=[0-9]*) ;;
  *) restore || true; fail 'HY2-USA delay test returned an invalid result; backup was restored' ;;
esac

safe_status \
  'RESULT=success' \
  'HY2_PROFILE=HY2-USA' \
  'HY2_PORT_HOPPING=enabled' \
  "$delay_output" \
  "BACKUP_DIR=$backup_dir" \
  'HY2_PROFILE_UPDATE=success'
