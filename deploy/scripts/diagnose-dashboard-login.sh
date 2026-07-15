#!/bin/sh
set -eu

DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
DASHBOARD_CONTAINER=${DASHBOARD_CONTAINER:-vpn-dashboard}

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }

"$DOCKER_BIN" exec -i "$DASHBOARD_CONTAINER" python - <<'PY'
from __future__ import annotations

import datetime as dt
import sqlite3

connection = sqlite3.connect("/data/dashboard.sqlite3")
owners = connection.execute("SELECT COUNT(*) FROM dashboard_owners").fetchone()[0]
admins = connection.execute("SELECT COUNT(*) FROM dashboard_admins").fetchone()[0]
records = connection.execute("SELECT blocked_until FROM login_throttle_records").fetchall()
now = dt.datetime.now(dt.timezone.utc)
active = 0
for (value,) in records:
    if not value:
        continue
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    if parsed > now:
        active += 1
failed_logins = connection.execute(
    "SELECT COUNT(*) FROM audit_events WHERE action = 'login_failed' AND succeeded = 0"
).fetchone()[0]
print("RESULT=success")
print(f"OWNER_COUNT={owners}")
print(f"ADMIN_COUNT={admins}")
print(f"THROTTLE_RECORDS={len(records)}")
print(f"THROTTLE_ACTIVE={active}")
print(f"FAILED_LOGIN_AUDITS={failed_logins}")
PY
