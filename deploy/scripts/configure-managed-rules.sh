#!/bin/sh
# One-time, reversible bootstrap for the dashboard's finite GeoSite/GeoIP
# policy catalogue. It relies on the exact current gateway anchors and refuses
# to guess if the Mihomo config has drifted.
set -eu

GATEWAY_DIR=${GATEWAY_DIR:-/volume1/docker/vpn-gateway}
CONFIG=${CONFIG:-$GATEWAY_DIR/mihomo/config.yaml}
RULES_DIR=${RULES_DIR:-$GATEWAY_DIR/mihomo/rules}
BACKUP_DIR=${BACKUP_DIR:-$GATEWAY_DIR/backups}
DASHBOARD_SECRET_FILE=${DASHBOARD_SECRET_FILE:-/volume1/docker/vpn-dashboard/deploy/secrets/dashboard_encryption_key}
DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
MIHOMO_CONTAINER=${MIHOMO_CONTAINER:-vpn-mihomo}
DASHBOARD_CONTAINER=${DASHBOARD_CONTAINER:-vpn-dashboard}
APP_UID=${APP_UID:-10001}
APP_GID=${APP_GID:-10001}

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -f "$CONFIG" ] || { echo 'Mihomo config is missing' >&2; exit 1; }
[ -f "$DASHBOARD_SECRET_FILE" ] || { echo 'dashboard backup secret is missing' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo 'openssl is unavailable' >&2; exit 1; }
[ -x /usr/bin/python3 ] || { echo 'python3 is unavailable' >&2; exit 1; }
"$DOCKER_BIN" inspect "$MIHOMO_CONTAINER" >/dev/null 2>&1 || { echo 'Mihomo container is missing' >&2; exit 1; }

umask 077
mkdir -p "$RULES_DIR" "$BACKUP_DIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
archive="$BACKUP_DIR/mihomo-before-managed-rules-$stamp.tar.enc"
tar -C "$GATEWAY_DIR" -cf - mihomo/config.yaml mihomo/rules | openssl enc -aes-256-cbc -pbkdf2 -salt -pass "file:$DASHBOARD_SECRET_FILE" -out "$archive"
chmod 0600 "$archive"

for file in managed-direct.txt managed-fallback.txt managed-wg-imp.txt managed-hy2-nl.txt; do
  [ -f "$RULES_DIR/$file" ] || : > "$RULES_DIR/$file"
  chown "$APP_UID:$APP_GID" "$RULES_DIR/$file"
  chmod 0660 "$RULES_DIR/$file"
done

config_mode=$(stat -c '%a' "$CONFIG")
config_owner=$(stat -c '%u:%g' "$CONFIG")
previous=$(mktemp "$GATEWAY_DIR/mihomo/.config.managed.previous.XXXXXX")
cp -p "$CONFIG" "$previous"
cleanup() { rm -f "$previous"; }
trap cleanup EXIT INT TERM

/usr/bin/python3 - "$CONFIG" <<'PY'
from pathlib import Path
import os
import sys
import tempfile

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
provider_anchor = "    path: ./rules/direct.txt\n"
provider_block = """    path: ./rules/direct.txt

  managed-direct:
    type: file
    behavior: classical
    format: text
    path: ./rules/managed-direct.txt
  managed-wg-imp:
    type: file
    behavior: classical
    format: text
    path: ./rules/managed-wg-imp.txt
  managed-hy2-nl:
    type: file
    behavior: classical
    format: text
    path: ./rules/managed-hy2-nl.txt
  managed-fallback:
    type: file
    behavior: classical
    format: text
    path: ./rules/managed-fallback.txt
"""
rule_anchor = "  - RULE-SET,direct-custom,DIRECT\n"
rule_block = """  - RULE-SET,direct-custom,DIRECT
  - RULE-SET,managed-direct,DIRECT
  - RULE-SET,managed-wg-imp,WG-IMP
  - RULE-SET,managed-hy2-nl,HY2-NL
  - RULE-SET,managed-fallback,VPS-FALLBACK
"""
provider_names = ("managed-direct", "managed-wg-imp", "managed-hy2-nl", "managed-fallback")
provider_count = sum(f"  {name}:\n" in text for name in provider_names)
rule_lines = (
    "  - RULE-SET,managed-direct,DIRECT\n",
    "  - RULE-SET,managed-wg-imp,WG-IMP\n",
    "  - RULE-SET,managed-hy2-nl,HY2-NL\n",
    "  - RULE-SET,managed-fallback,VPS-FALLBACK\n",
)
rule_count = sum(line in text for line in rule_lines)
if provider_count not in {0, len(provider_names)}:
    raise SystemExit("managed providers are only partially configured")
if rule_count not in {0, len(rule_lines)}:
    raise SystemExit("managed rules are only partially configured")
if provider_count == 0:
    if provider_anchor not in text:
        raise SystemExit("managed-provider anchor not found")
    text = text.replace(provider_anchor, provider_block, 1)
if rule_count == 0:
    if rule_anchor not in text:
        raise SystemExit("managed-rule anchor not found")
    text = text.replace(rule_anchor, rule_block, 1)
fd, temporary_name = tempfile.mkstemp(prefix=".config.managed.next.", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary_name, path)
except BaseException:
    Path(temporary_name).unlink(missing_ok=True)
    raise
PY
chown "$config_owner" "$CONFIG"
chmod "$config_mode" "$CONFIG"

restore() {
  cp -p "$previous" "$CONFIG"
  "$DOCKER_BIN" restart "$MIHOMO_CONTAINER" >/dev/null || true
}

if ! "$DOCKER_BIN" exec "$MIHOMO_CONTAINER" /mihomo -t -d /root/.config/mihomo; then
  restore
  echo 'RESULT=failed' >&2
  echo 'REASON=Mihomo configuration validation failed; backup was restored' >&2
  exit 1
fi
if ! "$DOCKER_BIN" restart "$MIHOMO_CONTAINER" >/dev/null; then
  restore
  echo 'RESULT=failed' >&2
  echo 'REASON=Mihomo restart failed; backup was restored' >&2
  exit 1
fi
if "$DOCKER_BIN" inspect "$DASHBOARD_CONTAINER" >/dev/null 2>&1; then
  "$DOCKER_BIN" restart "$DASHBOARD_CONTAINER" >/dev/null
fi

echo 'RESULT=success'
echo 'MANAGED_RULE_PROVIDERS=ready'
echo "ENCRYPTED_BACKUP=$(basename "$archive")"
echo 'OPENAI_DEFAULT=VPS-FALLBACK on dashboard restart'
