#!/bin/sh
# Install the DSM boot-task payload atomically. This script is invoked through
# sudo by the matching PowerShell installer, never by the DSM task itself.
set -eu

source_script=${1:?source script path is required}
target_dir=/volume1/docker/vpn-gateway/scripts
target_script="$target_dir/mihomo-boot-autostart.sh"

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -f "$source_script" ] || { echo "source script is missing: $source_script" >&2; exit 1; }

mkdir -p "$target_dir" /volume1/docker/vpn-gateway/logs
temp_script="$target_dir/.mihomo-boot-autostart.$$.tmp"
cleanup() { rm -f "$temp_script"; }
trap cleanup EXIT HUP INT TERM

cp "$source_script" "$temp_script"
chown root:root "$temp_script"
chmod 0750 "$temp_script"
/bin/sh -n "$temp_script"
mv "$temp_script" "$target_script"
chown root:root "$target_dir" /volume1/docker/vpn-gateway/logs "$target_script"
chmod 0750 "$target_dir" /volume1/docker/vpn-gateway/logs "$target_script"

echo "BOOT_SCRIPT=installed:$target_script"
echo 'DSM_TASK_EVENT=Boot-up'
echo 'DSM_TASK_USER=root'
echo "DSM_TASK_COMMAND=/bin/sh $target_script"
