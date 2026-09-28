#!/bin/sh
# Verify one exact snapshot in a fresh private directory, never production.
set -eu
[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ "$#" = 1 ] || { echo 'one exact snapshot id is required' >&2; exit 2; }
case "$1" in *[!a-f0-9]*|'') echo 'invalid snapshot id' >&2; exit 2 ;; esac
[ "${#1}" = 64 ] || { echo 'full snapshot id is required' >&2; exit 2; }
[ -f /usr/local/lib/vpn-dashboard-maintenance/maintenance/backup_cli.py ] || { echo 'RESTORE_VERIFY=blocked; reviewed maintenance backup is not installed; no files changed' >&2; exit 1; }
cd /usr/local/lib/vpn-dashboard-maintenance
unset PYTHONPATH PYTHONHOME
exec /usr/bin/python3 -E -B -m maintenance.backup_cli verify "$1"
