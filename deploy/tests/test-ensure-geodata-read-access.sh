#!/bin/sh
# The dashboard UID must be granted inherited read access to GeoData files.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
SCRIPT="$ROOT/deploy/scripts/ensure-geodata-read-access.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/mihomo"
: > "$TMP/mihomo/GeoIP.dat"
: > "$TMP/mihomo/GeoSite.dat"

cat > "$TMP/id" <<'EOF'
#!/bin/sh
printf '0\n'
EOF
chmod 700 "$TMP/id"

cat > "$TMP/synoacltool" <<'EOF'
#!/bin/sh
set -eu
case "$1" in
  -getace) exit 0 ;;
  -addace) printf '%s|%s|%s\n' "$1" "$2" "$3" >> "$ACL_LOG" ;;
  *) exit 64 ;;
esac
EOF
chmod 700 "$TMP/synoacltool"

output=$(PATH="$TMP:$PATH" \
  ACL_LOG="$TMP/acl.log" \
  MIHOMO_DIR="$TMP/mihomo" \
  SYNOACL_BIN="$TMP/synoacltool" \
  DASHBOARD_UID=10001 \
  /bin/sh "$SCRIPT")

[ "$output" = 'GEODATA_ACL=ready' ]
grep -Fqx -- "-addace|$TMP/mihomo|user:10001:allow:r-x---a-R-c--:fd--" "$TMP/acl.log"
grep -Fqx -- "-addace|$TMP/mihomo/GeoIP.dat|user:10001:allow:r-----a-R-c--:----" "$TMP/acl.log"
grep -Fqx -- "-addace|$TMP/mihomo/GeoSite.dat|user:10001:allow:r-----a-R-c--:----" "$TMP/acl.log"
printf '%s\n' 'PASS test-ensure-geodata-read-access'
