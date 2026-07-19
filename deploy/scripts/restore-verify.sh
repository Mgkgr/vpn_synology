#!/bin/sh
set -eu

RESTIC_REPOSITORY=${RESTIC_REPOSITORY:?RESTIC_REPOSITORY is required}
RESTIC_PASSWORD_FILE=${RESTIC_PASSWORD_FILE:?RESTIC_PASSWORD_FILE is required}
TARGET_DIR=${TARGET_DIR:-/volume1/docker/vpn-gateway/backups/dashboard/restore-check}

command -v restic >/dev/null || { echo 'restic is required' >&2; exit 1; }
command -v sqlite3 >/dev/null || { echo 'sqlite3 is required' >&2; exit 1; }
rm -rf "$TARGET_DIR"
mkdir -p "$TARGET_DIR"
RESTIC_PASSWORD_FILE="$RESTIC_PASSWORD_FILE" restic check
RESTIC_PASSWORD_FILE="$RESTIC_PASSWORD_FILE" restic restore latest --target "$TARGET_DIR"
DB=$(find "$TARGET_DIR" -name dashboard.sqlite3 -type f | head -n 1)
[ -n "$DB" ] || { echo 'dashboard.sqlite3 is missing from backup' >&2; exit 1; }
[ "$(sqlite3 "$DB" 'PRAGMA integrity_check;')" = ok ] || { echo 'SQLite integrity check failed' >&2; exit 1; }
find "$TARGET_DIR" -name dashboard.env -type f | grep -q . || { echo 'dashboard.env is missing from backup' >&2; exit 1; }
DASHBOARD_SECRETS=$(find "$TARGET_DIR" -name dashboard-secrets.tar -type f | head -n 1)
[ -n "$DASHBOARD_SECRETS" ] || { echo 'dashboard secrets archive is missing from backup' >&2; exit 1; }
tar -tf "$DASHBOARD_SECRETS" | grep -q '^secrets/dashboard_encryption_key$' || { echo 'dashboard encryption secret is missing from backup' >&2; exit 1; }
GATEWAY_ARCHIVE=$(find "$TARGET_DIR" -name gateway-persistence.tar -type f | head -n 1)
[ -n "$GATEWAY_ARCHIVE" ] || { echo 'gateway persistence archive is missing from backup' >&2; exit 1; }
tar -tf "$GATEWAY_ARCHIVE" | grep -Eq '^(wireguard|wg-easy)/' || { echo 'WireGuard persistence is missing from backup' >&2; exit 1; }
rm -rf "$TARGET_DIR"
