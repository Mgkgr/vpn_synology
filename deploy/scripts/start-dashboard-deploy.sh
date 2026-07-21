#!/bin/sh
# Root-only launcher for the detached dashboard deploy. Keeping the `nohup`
# operation in this file avoids PowerShell/OpenSSH nested-quote loss.
set -eu

PROJECT_DIR=${PROJECT_DIR:-/volume1/docker/vpn-dashboard}
RUN_ID=${1:?run id is required}

case "$RUN_ID" in *[!A-Za-z0-9-]*|'') echo 'invalid run id' >&2; exit 2 ;; esac
[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -f "$PROJECT_DIR/deploy/scripts/run-dashboard-deploy.sh" ] || {
  echo 'detached deploy wrapper is missing' >&2
  exit 1
}

nohup /bin/sh "$PROJECT_DIR/deploy/scripts/run-dashboard-deploy.sh" "$RUN_ID" >/dev/null 2>&1 &
echo "DEPLOY_RUN_ID=$RUN_ID"
