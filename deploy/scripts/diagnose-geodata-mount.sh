#!/bin/sh
# Read-only diagnosis of the GeoData bind mounts used by vpn-dashboard.
# It prints only filenames, ownership and file metadata; no secrets or configs.
set -eu

DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
GATEWAY_DIR=${GATEWAY_DIR:-/volume1/docker/vpn-gateway/mihomo}
DASHBOARD_CONTAINER=${DASHBOARD_CONTAINER:-vpn-dashboard}

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }
"$DOCKER_BIN" inspect "$DASHBOARD_CONTAINER" >/dev/null 2>&1 || {
  echo "dashboard container is unavailable: $DASHBOARD_CONTAINER" >&2
  exit 1
}

echo 'RESULT=success'
echo 'GEODATA_MOUNT_DIAGNOSTIC=read_only'

echo 'HOST_FILES=begin'
for filename in GeoIP.dat GeoSite.dat; do
  path="$GATEWAY_DIR/$filename"
  if [ -f "$path" ] && [ -r "$path" ]; then
    if stat -c 'HOST_FILE=%n|SIZE=%s|MTIME=%y|MODE=%a|OWNER=%U:%G' "$path" 2>/dev/null; then
      :
    else
      ls -ln "$path" | sed 's/^/HOST_FILE=/'
    fi
  else
    echo "HOST_FILE=$path|STATE=missing_or_unreadable"
  fi
done
echo 'HOST_FILES=end'

echo 'DASHBOARD_MOUNTS=begin'
"$DOCKER_BIN" inspect -f '{{range .Mounts}}{{if or (eq .Destination "/geodata/GeoIP.dat") (eq .Destination "/geodata/GeoSite.dat")}}{{printf "MOUNT=SOURCE=%s|DESTINATION=%s|TYPE=%s|RW=%t\n" .Source .Destination .Type .RW}}{{end}}{{end}}' "$DASHBOARD_CONTAINER"
echo 'DASHBOARD_MOUNTS=end'

echo 'CONTAINER_FILES=begin'
"$DOCKER_BIN" exec -i "$DASHBOARD_CONTAINER" python - <<'PY'
import os
from pathlib import Path

directory = Path(os.environ.get("GEODATA_DIR", "/geodata"))
print(f"GEODATA_DIR={directory}|IS_DIR={directory.is_dir()}")
for filename in ("GeoIP.dat", "GeoSite.dat"):
    path = directory / filename
    try:
        stat = path.stat()
    except OSError as error:
        print(f"CONTAINER_FILE={filename}|STATE=unavailable|ERROR={type(error).__name__}")
        continue
    print(
        f"CONTAINER_FILE={filename}|IS_FILE={path.is_file()}|READABLE={os.access(path, os.R_OK)}"
        f"|SIZE={stat.st_size}|MTIME={int(stat.st_mtime)}"
    )

try:
    from app.collectors import LocalGeoFileStore

    metadata = LocalGeoFileStore().metadata(directory)
except OSError as error:
    print(f"COLLECTOR_METADATA=unavailable|ERROR={type(error).__name__}")
else:
    print("COLLECTOR_METADATA=ok|FILES=" + ",".join(item.filename for item in metadata))
PY
echo 'CONTAINER_FILES=end'
