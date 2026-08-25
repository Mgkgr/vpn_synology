#!/bin/sh
# The diagnostic must cover the exact Russian services that frequently need
# DIRECT routing and must remain read-only.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
SCRIPT="$ROOT/deploy/scripts/diagnose-russian-services-routing.sh"
WRAPPER="$ROOT/deploy/diagnose-russian-services-routing.ps1"

[ -f "$SCRIPT" ] || { echo "missing Russian-services diagnostic: $SCRIPT" >&2; exit 1; }
[ -f "$WRAPPER" ] || { echo "missing Russian-services wrapper: $WRAPPER" >&2; exit 1; }

grep -Fqx "echo 'RUSSIAN_SERVICES_DIAGNOSTIC=read_only'" "$SCRIPT"
grep -Fq "'ozon', 'avito', 'yandex', 'category-ecommerce-ru', 'category-retail-ru'" "$SCRIPT"
grep -Fq 'managed-direct.txt' "$SCRIPT"
grep -Fq 'managed-wg-imp.txt' "$SCRIPT"
grep -Fq 'managed-hy2-nl.txt' "$SCRIPT"
grep -Fq 'managed-fallback.txt' "$SCRIPT"
grep -Fq 'Ozon' "$SCRIPT"
grep -Fq 'Avito' "$SCRIPT"
grep -Fq 'Yandex' "$SCRIPT"
grep -Fq 'SOURCE_ADDRESS' "$SCRIPT"
grep -Fq 'WatchSeconds' "$WRAPPER"
grep -Fq 'SourceAddress' "$WRAPPER"
! grep -Eq '(^|[[:space:]])(rm|mv|cp|sed[[:space:]]+-i|curl[[:space:]].*-X[[:space:]]+(POST|PUT|PATCH|DELETE))([[:space:]]|$)' "$SCRIPT"

printf '%s\n' 'PASS test-diagnose-russian-services-routing'
