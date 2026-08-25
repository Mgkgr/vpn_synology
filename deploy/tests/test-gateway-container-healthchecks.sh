#!/bin/sh
# The gateway source must define inexpensive runtime health checks for the
# two containers Docker previously reported as "none".
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
COMPOSE="$ROOT/deploy/gateway/compose.yaml"
APPLIER="$ROOT/deploy/scripts/apply-gateway-healthchecks.sh"

[ -f "$COMPOSE" ] || { echo "missing gateway compose source: $COMPOSE" >&2; exit 1; }
[ -f "$APPLIER" ] || { echo "missing healthcheck applier: $APPLIER" >&2; exit 1; }

mihomo=$(awk '/^  mihomo:$/,/^  metacubexd:$/' "$COMPOSE")
metacubexd=$(awk '/^  metacubexd:$/,/^  uptime-kuma:$/' "$COMPOSE")

printf '%s\n' "$mihomo" | grep -Fqx '    healthcheck:'
printf '%s\n' "$mihomo" | grep -Fq 'CMD-SHELL'
printf '%s\n' "$mihomo" | grep -Fq '/proc/net/tcp'
printf '%s\n' "$mihomo" | grep -Fq ':2393 '
printf '%s\n' "$mihomo" | grep -Fqx '      interval: 30s'
printf '%s\n' "$metacubexd" | grep -Fqx '    healthcheck:'
printf '%s\n' "$metacubexd" | grep -Fq "require('http')"
printf '%s\n' "$metacubexd" | grep -Fq '127.0.0.1'
printf '%s\n' "$metacubexd" | grep -Fqx '      interval: 30s'
grep -Fq 'EXPECTED_COMPOSE_SHA256=' "$APPLIER"
grep -Fq 'HEALTHCHECK_APPLY=success' "$APPLIER"
grep -Fq 'MIHOMO_CONTROLLER=ready' "$APPLIER"

printf '%s\n' 'PASS test-gateway-container-healthchecks'
