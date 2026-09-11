#!/bin/sh
# Regression: route diagnostics may inspect all clients, so an empty source
# filter is valid; a non-IPv4 filter must fail before reaching Mihomo.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/gateway/mihomo/rules"

cat > "$TMP/bin/id" <<'EOF'
#!/bin/sh
printf '0\n'
EOF
chmod 700 "$TMP/bin/id"

cat > "$TMP/bin/docker" <<'EOF'
#!/bin/sh
set -eu
for argument in "$@"; do
  if [ "$argument" = '-' ]; then
    cat >/dev/null
    break
  fi
done
exit 0
EOF
chmod 700 "$TMP/bin/docker"

cat > "$TMP/gateway/mihomo/config.yaml" <<'EOF'
rules:
  - RULE-SET,direct-custom,DIRECT
  - MATCH,VPS-FALLBACK
EOF
touch "$TMP/gateway/mihomo/rules/direct.txt"

run() {
  PATH="$TMP/bin:$PATH" \
    DOCKER_BIN="$TMP/bin/docker" \
    GATEWAY_DIR="$TMP/gateway" \
    CONFIG="$TMP/gateway/mihomo/config.yaml" \
    RULES_DIR="$TMP/gateway/mihomo/rules" \
    /bin/sh "$@"
}

domain_output=$(run "$ROOT/deploy/scripts/diagnose-domain-route.sh" example.com 0 '')
printf '%s\n' "$domain_output" | grep -Fqx 'RESULT=success'

kinopoisk_output=$(run "$ROOT/deploy/scripts/diagnose-kinopoisk-routing.sh" 0 '')
printf '%s\n' "$kinopoisk_output" | grep -Fqx 'RESULT=success'

if run "$ROOT/deploy/scripts/diagnose-domain-route.sh" example.com 0 '10.66.0.999' >/dev/null 2>&1; then
  echo 'domain route diagnostic accepted an out-of-range address' >&2
  exit 1
fi
if run "$ROOT/deploy/scripts/diagnose-kinopoisk-routing.sh" 0 '10.66.0.999' >/dev/null 2>&1; then
  echo 'kinopoisk diagnostic accepted an out-of-range address' >&2
  exit 1
fi

printf '%s\n' 'PASS test-diagnose-route-source-address'
