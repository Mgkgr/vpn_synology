#!/bin/sh
# Replace only the HY2-NL outbound and its separate secret file. The existing
# config is edited as a single block so managed rules, GeoData and WG-IMP keep
# their current state. A failed check restores both changed files.
set -eu

SOURCE_PROFILE=${1:?source Hysteria2 profile path is required}
PROJECT_DIR=${PROJECT_DIR:-/volume1/docker/vpn-gateway}
TARGET_PROFILE="$PROJECT_DIR/secrets/hysteria2.env"
CONFIG="$PROJECT_DIR/mihomo/config.yaml"
BACKUP_ROOT="$PROJECT_DIR/backups"
DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
MIHOMO_CONTAINER=${MIHOMO_CONTAINER:-vpn-mihomo}
DASHBOARD_CONTAINER=${DASHBOARD_CONTAINER:-vpn-dashboard}
WAIT_SECONDS=${WAIT_SECONDS:-45}

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }
[ -r "$SOURCE_PROFILE" ] || { echo 'source Hysteria2 profile is unreadable' >&2; exit 1; }
[ -f "$TARGET_PROFILE" ] || { echo 'installed Hysteria2 profile is missing' >&2; exit 1; }
[ -f "$CONFIG" ] || { echo 'Mihomo config is missing' >&2; exit 1; }

for container in "$MIHOMO_CONTAINER" "$DASHBOARD_CONTAINER"; do
  "$DOCKER_BIN" inspect "$container" >/dev/null 2>&1 || { echo "required container is missing: $container" >&2; exit 1; }
done

for key in HY2_SERVER HY2_PORT HY2_PORTS HY2_PASSWORD HY2_OBFS_PASSWORD HY2_SNI; do
  grep -q "^$key=" "$SOURCE_PROFILE" || { echo "source Hysteria2 profile is missing $key" >&2; exit 1; }
done

backup_dir="$BACKUP_ROOT/hy2-profile-$(date -u +%Y%m%dT%H%M%SZ)"
staged_profile="$PROJECT_DIR/.hysteria2.hy2.$$"
backup_ready=0
changed=0

cleanup() {
  rm -f "$staged_profile"
}

reload_mihomo() {
  "$DOCKER_BIN" exec -i "$DASHBOARD_CONTAINER" python - <<'PY'
import os
from pathlib import Path
from urllib.request import Request, urlopen

base = os.environ.get("MIHOMO_URL", "http://vpn-wireguard:9091").rstrip("/")
secret_file = os.environ.get("MIHOMO_API_SECRET_FILE")
if not secret_file:
    raise RuntimeError("Mihomo controller secret file is not configured")
secret = Path(secret_file).read_text(encoding="utf-8").strip()
if not secret:
    raise RuntimeError("Mihomo controller secret is empty")
request = Request(
    f"{base}/configs?force=true",
    data=b'{"path":"","payload":""}',
    method="PUT",
    headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
)
with urlopen(request, timeout=10) as response:
    if not 200 <= response.status < 300:
        raise RuntimeError("Mihomo controller rejected config reload")
PY
}

replace_hy2_block() {
  python3 - "$SOURCE_PROFILE" "$CONFIG" <<'PY'
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

profile_path = Path(sys.argv[1])
config_path = Path(sys.argv[2])
expected = (
    "HY2_SERVER",
    "HY2_PORT",
    "HY2_PORTS",
    "HY2_PASSWORD",
    "HY2_OBFS_PASSWORD",
    "HY2_SNI",
)
values = {}
for line in profile_path.read_text(encoding="utf-8").splitlines():
    match = re.fullmatch(r"([A-Z0-9_]+)='([^'\r\n]*)'", line)
    if not match:
        raise RuntimeError("source Hysteria2 profile has an invalid format")
    key, value = match.groups()
    if key not in expected or key in values or not value:
        raise RuntimeError("source Hysteria2 profile has invalid fields")
    values[key] = value
if tuple(values) != expected:
    raise RuntimeError("source Hysteria2 profile fields are incomplete or reordered")
if not re.fullmatch(r"[A-Za-z0-9.-]+", values["HY2_SERVER"]):
    raise RuntimeError("Hysteria2 server is invalid")
if not re.fullmatch(r"\d{1,5}", values["HY2_PORT"]) or not 1 <= int(values["HY2_PORT"]) <= 65535:
    raise RuntimeError("Hysteria2 port is invalid")
range_match = re.fullmatch(r"(\d{1,5})-(\d{1,5})", values["HY2_PORTS"])
if not range_match or not 1 <= int(range_match.group(1)) <= int(range_match.group(2)) <= 65535:
    raise RuntimeError("Hysteria2 port range is invalid")

def quoted(value):
    return value.replace("\\", "\\\\").replace('"', '\\"')

lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
start = next((index for index, line in enumerate(lines) if line.rstrip("\r\n") == "  - name: HY2-NL"), None)
if start is None:
    raise RuntimeError("HY2-NL outbound is missing from the current config")
end = next(
    (index for index in range(start + 1, len(lines)) if re.match(r"^  - name: ", lines[index])),
    len(lines),
)
replacement = [
    "  - name: HY2-NL\n",
    "    type: hysteria2\n",
    f'    server: "{quoted(values["HY2_SERVER"])}"\n',
    f'    port: {values["HY2_PORT"]}\n',
    f'    ports: "{values["HY2_PORTS"]}"\n',
    f'    password: "{quoted(values["HY2_PASSWORD"])}"\n',
    "    obfs: salamander\n",
    f'    obfs-password: "{quoted(values["HY2_OBFS_PASSWORD"])}"\n',
    f'    sni: "{quoted(values["HY2_SNI"])}"\n',
    "    skip-cert-verify: false\n",
    "    udp: true\n",
]
result = lines[:start] + replacement + lines[end:]
original = config_path.stat()
fd, temp_name = tempfile.mkstemp(prefix=".config.yaml.hy2.", dir=config_path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.writelines(result)
    os.chown(temp_name, original.st_uid, original.st_gid)
    os.chmod(temp_name, stat.S_IMODE(original.st_mode))
    os.replace(temp_name, config_path)
finally:
    if os.path.exists(temp_name):
        os.unlink(temp_name)
PY
}

restore() {
  reason=$1
  if [ "$backup_ready" = 1 ] && [ "$changed" = 1 ]; then
    cp -p "$backup_dir/hysteria2.env" "$TARGET_PROFILE"
    cp -p "$backup_dir/config.yaml" "$CONFIG"
    reload_mihomo >/dev/null 2>&1 || "$DOCKER_BIN" restart "$MIHOMO_CONTAINER" >/dev/null 2>&1 || true
    echo 'ROLLBACK=applied' >&2
  fi
  echo "REASON=$reason" >&2
  exit 1
}

trap cleanup EXIT HUP INT TERM

"$DOCKER_BIN" exec "$MIHOMO_CONTAINER" /mihomo -t -d /root/.config/mihomo >/dev/null || {
  echo 'REASON=the current Mihomo configuration is invalid; no change was made' >&2
  exit 1
}

umask 077
mkdir -p "$backup_dir"
cp -p "$TARGET_PROFILE" "$backup_dir/hysteria2.env"
cp -p "$CONFIG" "$backup_dir/config.yaml"
chmod 600 "$backup_dir/hysteria2.env" "$backup_dir/config.yaml"
backup_ready=1

cp "$SOURCE_PROFILE" "$staged_profile"
chmod 600 "$staged_profile"
chown root:root "$staged_profile"
mv "$staged_profile" "$TARGET_PROFILE"
changed=1

replace_hy2_block || restore 'HY2-NL config block update failed'
"$DOCKER_BIN" exec "$MIHOMO_CONTAINER" /mihomo -t -d /root/.config/mihomo >/dev/null || restore 'Mihomo configuration validation failed'
reload_mihomo || restore 'Mihomo controller rejected the new configuration'

probe_hy2() {
  # Run an authenticated controller proxy_delay against HY2 alone. This does
  # not change the fallback selection and never disables WG-IMP.
  "$DOCKER_BIN" exec -i "$DASHBOARD_CONTAINER" python - <<'PY'
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

base = os.environ.get("MIHOMO_URL", "http://vpn-wireguard:9091").rstrip("/")
secret_file = os.environ.get("MIHOMO_API_SECRET_FILE")
if not secret_file:
    raise RuntimeError("Mihomo controller secret file is not configured")
secret = Path(secret_file).read_text(encoding="utf-8").strip()
if not secret:
    raise RuntimeError("Mihomo controller secret is empty")
query = urlencode({"url": "https://cp.cloudflare.com/generate_204", "timeout": "10000"})
request = Request(
    f"{base}/proxies/HY2-NL/delay?{query}",
    headers={"Authorization": f"Bearer {secret}"},
)
with urlopen(request, timeout=15) as response:
    payload = json.loads(response.read().decode("utf-8"))
delay = payload.get("delay")
if not isinstance(delay, int) or delay <= 0:
    raise RuntimeError("Hysteria2 delay probe did not report a usable delay")
print(f"HY2_DELAY_MS={delay}")
PY
}

elapsed=0
delay_result=''
while [ "$elapsed" -lt "$WAIT_SECONDS" ]; do
  if delay_result=$(probe_hy2 2>/dev/null); then
    break
  fi
  sleep 1
  elapsed=$((elapsed + 1))
done
[ -n "$delay_result" ] || restore 'new Hysteria2 profile did not pass the authenticated delay probe'

printf '%s\n' "$delay_result"
echo 'MIHOMO_CONFIG=valid'
echo 'HY2_PORT_HOPPING=enabled'
echo "BACKUP=$backup_dir"
echo 'HY2_PROFILE_UPDATE=success'
