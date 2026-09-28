#!/bin/sh
# Uses only the root-installed discovery manifest and global mutation lease.
set -eu
[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ "$#" = 0 ] || { echo 'no arguments accepted' >&2; exit 2; }
[ -f /usr/local/lib/vpn-dashboard-maintenance/maintenance/backup_cli.py ] || { echo 'BACKUP=blocked; reviewed maintenance backup is not installed; no files changed' >&2; exit 1; }
cd /usr/local/lib/vpn-dashboard-maintenance
unset PYTHONPATH PYTHONHOME
exec /usr/bin/python3 -E -B -m maintenance.backup_cli backup
