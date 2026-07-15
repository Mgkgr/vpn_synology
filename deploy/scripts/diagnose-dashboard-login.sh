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
active_sessions = connection.execute(
    "SELECT COUNT(*) FROM dashboard_sessions WHERE expires_at > ?",
    (now.isoformat(),),
).fetchone()[0]
recent_events = connection.execute(
    """
    SELECT observed_at, action, succeeded
    FROM audit_events
    WHERE action IN ('login', 'login_failed')
    ORDER BY id DESC
    LIMIT 8
    """
).fetchall()
print("RESULT=success")
print(f"OWNER_COUNT={owners}")
print(f"ADMIN_COUNT={admins}")
print(f"THROTTLE_RECORDS={len(records)}")
print(f"THROTTLE_ACTIVE={active}")
print(f"FAILED_LOGIN_AUDITS={failed_logins}")
print(f"ACTIVE_SESSIONS={active_sessions}")
for index, (observed_at, action, succeeded) in enumerate(recent_events, start=1):
    status = "success" if succeeded else "failed"
    print(f"LOGIN_EVENT_{index}={observed_at}|{action}|{status}")
PY

# This uses a deliberately invalid payload.  A 422 response confirms that the
# running application exposes the login route without attempting a real login
# or creating an audit/throttle entry.
"$DOCKER_BIN" exec "$DASHBOARD_CONTAINER" python - <<'PY'
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

request = Request(
    "http://127.0.0.1:8080/api/auth/login",
    data=b"{}",
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urlopen(request, timeout=5) as response:
        print(f"INTERNAL_LOGIN_ROUTE_STATUS={response.status}")
except HTTPError as error:
    print(f"INTERNAL_LOGIN_ROUTE_STATUS={error.code}")
except URLError as error:
    print("INTERNAL_LOGIN_ROUTE_STATUS=unreachable")
    print(f"INTERNAL_LOGIN_ROUTE_REASON={type(error.reason).__name__}")
PY

# Uvicorn access lines do not include request payloads.  Keep only the login
# endpoint and its status codes, so the output is safe to paste into support.
login_lines=$("$DOCKER_BIN" logs --tail 300 "$DASHBOARD_CONTAINER" 2>&1 | grep 'POST /api/auth/login' | tail -n 8 || true)
if [ -n "$login_lines" ]; then
  printf '%s\n' "LOGIN_ACCESS_LOGS=begin"
  printf '%s\n' "$login_lines"
  printf '%s\n' "LOGIN_ACCESS_LOGS=end"
else
  printf '%s\n' "LOGIN_ACCESS_LOGS=none"
fi
