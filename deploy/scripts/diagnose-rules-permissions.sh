#!/bin/sh
set -eu

DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
RULES_DIR=${MIHOMO_RULES_DIR:-/volume1/docker/vpn-gateway/mihomo/rules}
DIRECT_FILE="$RULES_DIR/direct.txt"

echo 'RULES_HOST_METADATA=begin'
for path in /volume1 /volume1/docker /volume1/docker/vpn-gateway /volume1/docker/vpn-gateway/mihomo "$RULES_DIR" "$DIRECT_FILE"; do
  printf 'PATH=%s\n' "$path"
  ls -ld "$path" 2>&1 || true
  if [ -L "$path" ]; then
    printf 'SYMLINK_TARGET='
    readlink -f "$path" 2>&1 || true
  fi
  if command -v synoacltool >/dev/null 2>&1; then
    synoacltool -get "$path" 2>&1 || true
  fi
done
echo 'RULES_HOST_METADATA=end'

echo 'DASHBOARD_MOUNTS=begin'
"$DOCKER_BIN" inspect vpn-dashboard --format '{{range .Mounts}}{{println .Source .Destination .RW .Mode}}{{end}}' 2>&1 || true
echo 'DASHBOARD_MOUNTS=end'

echo 'DASHBOARD_RULES_ACCESS=begin'
"$DOCKER_BIN" exec -u 10001 vpn-dashboard sh -c '
  id
  ls -ld /gateway /gateway/rules /gateway/rules/direct.txt /geodata /geodata/GeoIP.dat /geodata/GeoSite.dat 2>&1 || true
  if test -r /gateway/rules/direct.txt; then
    echo DIRECT_READABLE=yes
  else
    echo DIRECT_READABLE=no
  fi
  if test -w /gateway/rules; then
    echo RULES_DIRECTORY_WRITABLE=yes
  else
    echo RULES_DIRECTORY_WRITABLE=no
  fi
' 2>&1 || true
echo 'DASHBOARD_RULES_ACCESS=end'
