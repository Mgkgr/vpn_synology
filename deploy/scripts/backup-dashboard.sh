#!/bin/sh
set -eu

PROJECT_DIR=${PROJECT_DIR:-/volume1/docker/vpn-dashboard}
GATEWAY_DIR=${GATEWAY_DIR:-/volume1/docker/vpn-gateway}
BACKUP_DIR=${BACKUP_DIR:-/volume1/docker/vpn-gateway/backups/dashboard}
RESTIC_REPOSITORY=${RESTIC_REPOSITORY:-$BACKUP_DIR/restic}
RESTIC_PASSWORD_FILE=${RESTIC_PASSWORD_FILE:?RESTIC_PASSWORD_FILE must point to a 0600 file}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
WORK_DIR="$BACKUP_DIR/work-$STAMP"

command -v restic >/dev/null || { echo 'restic is required' >&2; exit 1; }
command -v sqlite3 >/dev/null || { echo 'sqlite3 is required for a consistent snapshot' >&2; exit 1; }
[ "$(stat -c '%a' "$RESTIC_PASSWORD_FILE" 2>/dev/null || stat -f '%Lp' "$RESTIC_PASSWORD_FILE")" = 600 ] || { echo 'RESTIC_PASSWORD_FILE must be mode 0600' >&2; exit 1; }

umask 077
mkdir -p "$WORK_DIR" "$RESTIC_REPOSITORY"
trap 'rm -rf "$WORK_DIR"' EXIT INT TERM
sqlite3 "$PROJECT_DIR/deploy/data/dashboard.sqlite3" ".backup '$WORK_DIR/dashboard.sqlite3'"
cp "$PROJECT_DIR/deploy/dashboard.env" "$WORK_DIR/dashboard.env"
tar -C "$GATEWAY_DIR" -cf "$WORK_DIR/gateway-config.tar" mihomo wg-easy 2>/dev/null || tar -C "$GATEWAY_DIR" -cf "$WORK_DIR/gateway-config.tar" mihomo
RESTIC_PASSWORD_FILE="$RESTIC_PASSWORD_FILE" restic snapshots >/dev/null 2>&1 || RESTIC_PASSWORD_FILE="$RESTIC_PASSWORD_FILE" restic init
RESTIC_PASSWORD_FILE="$RESTIC_PASSWORD_FILE" restic backup --tag vpn-dashboard --tag "$STAMP" "$WORK_DIR"
RESTIC_PASSWORD_FILE="$RESTIC_PASSWORD_FILE" restic forget --tag vpn-dashboard --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune
