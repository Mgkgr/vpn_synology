#!/bin/sh
# Import only missing terminal DIRECT rules through the dashboard's RuleService.
# This keeps an immutable revision and audit event, atomically reloads Mihomo,
# and lets RuleService restore the previous provider on a reload failure.
set -eu

NORMALIZER=${1:?missing normalizer path}
SOURCE=${2:?missing import source path}
GATEWAY_DIR=${GATEWAY_DIR:-/volume1/docker/vpn-gateway}
DIRECT_FILE=${DIRECT_FILE:-$GATEWAY_DIR/mihomo/rules/direct.txt}
BACKUP_DIR=${BACKUP_DIR:-$GATEWAY_DIR/backups}
SECRET_FILE=${DASHBOARD_SECRET_FILE:-/volume1/docker/vpn-dashboard/deploy/secrets/dashboard_encryption_key}
DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
DASHBOARD_CONTAINER=${DASHBOARD_CONTAINER:-vpn-dashboard}
ACTOR=${DIRECT_IMPORT_ACTOR:-prometei}

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -x /usr/bin/python3 ] || { echo 'python3 is unavailable' >&2; exit 1; }
[ -f "$NORMALIZER" ] || { echo 'direct-rule normalizer is missing' >&2; exit 1; }
[ -f "$SOURCE" ] || { echo 'direct-rule import source is missing' >&2; exit 1; }
[ -f "$DIRECT_FILE" ] || { echo "DIRECT provider is missing: $DIRECT_FILE" >&2; exit 1; }
[ -f "$SECRET_FILE" ] || { echo 'dashboard backup secret is missing' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo 'openssl is unavailable' >&2; exit 1; }
"$DOCKER_BIN" inspect "$DASHBOARD_CONTAINER" >/dev/null 2>&1 || {
  echo 'dashboard container is missing' >&2
  exit 1
}

umask 077
normalized=$(mktemp /tmp/vpn-dashboard-direct-import.XXXXXX)
container_input=/tmp/direct-import.txt
cleanup() {
  rm -f "$normalized"
  "$DOCKER_BIN" exec "$DASHBOARD_CONTAINER" rm -f "$container_input" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

/usr/bin/python3 "$NORMALIZER" "$SOURCE" "$normalized"

mkdir -p "$BACKUP_DIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
archive="$BACKUP_DIR/direct-rules-before-import-$stamp.tar.enc"
tar -C "$GATEWAY_DIR" -cf - mihomo/rules/direct.txt | \
  openssl enc -aes-256-cbc -pbkdf2 -salt -pass "file:$SECRET_FILE" -out "$archive"
chmod 0600 "$archive"

"$DOCKER_BIN" exec -i "$DASHBOARD_CONTAINER" sh -c 'container_input=/tmp/direct-import.txt; umask 077; cat > "$container_input"' < "$normalized"
"$DOCKER_BIN" exec -i -e "DIRECT_IMPORT_ACTOR=$ACTOR" "$DASHBOARD_CONTAINER" python - <<'PY'
import asyncio
import os
from pathlib import Path

from app.main import create_collector_runtime

runtime = create_collector_runtime()
try:
    if runtime.container is None:
        raise RuntimeError("dashboard runtime is unavailable")
    service = runtime.container.rule_service
    if service is None:
        raise RuntimeError("DIRECT rule service is unavailable")

    current = service.preview_direct_rules(service.read_direct_rules())
    imported = service.preview_direct_rules(Path("/tmp/direct-import.txt").read_text(encoding="utf-8"))
    present = set(current.rules)
    additions = [rule for rule in imported.rules if rule not in present]

    print(f"DIRECT_RULES_CURRENT={current.rule_count}")
    print(f"DIRECT_IMPORT_RULES={imported.rule_count}")
    print(f"DIRECT_IMPORT_ALREADY_PRESENT={imported.rule_count - len(additions)}")
    print(f"DIRECT_IMPORT_ADDED={len(additions)}")
    if additions:
        merged = current.text.rstrip("\n") + "\n" + "\n".join(additions) + "\n"
        revision = asyncio.run(service.apply_direct_rules_async(merged, actor=os.environ["DIRECT_IMPORT_ACTOR"]))
        print(f"DIRECT_RULES_REVISION={revision.number}")
        print("MIHOMO_RELOAD=success")
    else:
        print("MIHOMO_RELOAD=not_needed")
finally:
    if runtime.close is not None:
        runtime.close()
PY

echo 'RESULT=success'
echo "ENCRYPTED_BACKUP=$(basename "$archive")"
