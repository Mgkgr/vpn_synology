#!/bin/sh
# Regression: SourceAddress is an optional filter, so an empty value is valid.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
SCRIPT="$ROOT/deploy/scripts/diagnose-lan3-routing.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/id" <<'EOF'
#!/bin/sh
printf '0\n'
EOF
chmod 700 "$TMP/id"
: > "$TMP/config.yaml"
: > "$TMP/direct.txt"

output=$(PATH="$TMP:$PATH" \
  VALIDATE_ONLY=1 \
  DOCKER_BIN=/bin/true \
  CONFIG="$TMP/config.yaml" \
  DIRECT_RULES="$TMP/direct.txt" \
  /bin/sh "$SCRIPT" 45 '')

[ "$output" = 'VALIDATION=ok' ]
printf '%s\n' 'PASS test-diagnose-lan3-routing-source-address'
