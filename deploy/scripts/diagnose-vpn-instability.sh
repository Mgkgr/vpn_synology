#!/bin/sh
# Read-only correlation snapshot for intermittent VPN interruptions. It prints
# only counters, state and names; credentials, endpoints and raw controller
# payloads deliberately never leave the NAS.
set -eu

PROJECT_DIR=${PROJECT_DIR:-/volume1/docker/vpn-gateway}
CONFIG=${CONFIG:-$PROJECT_DIR/mihomo/config.yaml}
DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
WIREGUARD_CONTAINER=${WIREGUARD_CONTAINER:-vpn-wireguard}
DASHBOARD_CONTAINER=${DASHBOARD_CONTAINER:-vpn-dashboard}
WATCH_SECONDS=${1:-45}
CLIENT_ADDRESS=${2:-10.66.0.4}
NOW_EPOCH=${NOW_EPOCH:-}

case "$WATCH_SECONDS" in ''|*[!0-9]*) echo 'watch duration must be an integer number of seconds' >&2; exit 1 ;; esac
[ "$WATCH_SECONDS" -le 60 ] || { echo 'watch duration must not exceed 60 seconds' >&2; exit 1; }
case "$CLIENT_ADDRESS" in
  10.66.*.*) ;;
  *) echo 'client address must be in the WireGuard subnet' >&2; exit 1 ;;
esac
[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }
[ -f "$CONFIG" ] || { echo 'Mihomo configuration is missing' >&2; exit 1; }
if [ -z "$NOW_EPOCH" ]; then
  NOW_EPOCH=$(date +%s)
fi
case "$NOW_EPOCH" in ''|*[!0-9]*) echo 'current time is unavailable' >&2; exit 1 ;; esac

TMP=$(mktemp -d /tmp/vpn-instability.XXXXXX)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

container_state() {
  container=$1
  "$DOCKER_BIN" inspect -f 'CONTAINER={{.Name}}|STATE={{.State.Status}}|RUNNING={{.State.Running}}|HEALTH={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|RESTARTS={{.RestartCount}}|OOM={{.State.OOMKilled}}|STARTED={{.State.StartedAt}}' "$container" 2>/dev/null ||
    echo "CONTAINER=/$container|STATE=missing|RUNNING=false|HEALTH=none|RESTARTS=unknown|OOM=unknown|STARTED=unknown"
}

fallback_config() {
  block=$(awk '
    /^  - name: VPS-FALLBACK$/ { capture = 1 }
    capture && /^  - name: / && $0 != "  - name: VPS-FALLBACK" { exit }
    capture { print }
  ' "$CONFIG")
  if [ -z "$block" ]; then
    echo 'FALLBACK_CONFIG=missing'
    return
  fi
  members=$(printf '%s\n' "$block" | awk '/^      - / { print $2 }' | paste -sd, -)
  interval=$(printf '%s\n' "$block" | awk '/^    interval: / { print $2; exit }')
  timeout=$(printf '%s\n' "$block" | awk '/^    timeout: / { print $2; exit }')
  failures=$(printf '%s\n' "$block" | awk '/^    max-failed-times: / { print $2; exit }')
  lazy=$(printf '%s\n' "$block" | awk '/^    lazy: / { print $2; exit }')
  printf 'FALLBACK_MEMBERS=%s\n' "${members:-unknown}"
  printf 'FALLBACK_INTERVAL_SECONDS=%s\n' "${interval:-default}"
  printf 'FALLBACK_TIMEOUT_MS=%s\n' "${timeout:-default}"
  printf 'FALLBACK_MAX_FAILED_TIMES=%s\n' "${failures:-default}"
  printf 'FALLBACK_LAZY=%s\n' "${lazy:-default}"
}

dashboard_observations() {
  "$DOCKER_BIN" exec -i "$DASHBOARD_CONTAINER" python - <<'PY'
import asyncio

from sqlalchemy import select

from app.db import create_session_factory, create_sqlite_engine
from app.mihomo import MihomoClient
from app.models import ProbeEvent, RouteEvent
from app.probe_targets import ProbeTargetService
from app.settings import Settings


def report_database() -> tuple[Settings, object]:
    settings = Settings.from_env()
    engine = create_sqlite_engine(settings.database_path)
    factory = create_session_factory(engine)
    try:
        targets = ProbeTargetService(factory).list_targets()
        enabled_count = sum(1 for target in targets if target.enabled)
        print(f"ENABLED_TARGET_COUNT={enabled_count}")
        print(f"AUTO_DELAY_CONCURRENCY_MAX={enabled_count * 2}")
        with factory() as session:
            rows = list(
                session.scalars(
                    select(ProbeEvent)
                    .where(ProbeEvent.target.in_(("WG-IMP", "HY2-USA")))
                    .order_by(ProbeEvent.observed_at.desc(), ProbeEvent.id.desc())
                    .limit(500)
                )
            )
            if rows:
                observed_at = rows[0].observed_at.isoformat()
                cycle = [row for row in rows if row.observed_at == rows[0].observed_at]
                print(f"LAST_PROBE_CYCLE_AT={observed_at}")
                print(f"LAST_PROBE_CYCLE_REQUESTS={len(cycle)}")
                print(f"LAST_PROBE_CYCLE_FAILURES={sum(not row.succeeded for row in cycle)}")
            else:
                print("LAST_PROBE_CYCLE_AT=none")
                print("LAST_PROBE_CYCLE_REQUESTS=0")
                print("LAST_PROBE_CYCLE_FAILURES=0")
            switch = session.scalar(
                select(RouteEvent)
                .where(RouteEvent.route == "VPS-FALLBACK", RouteEvent.action == "selected_outbound_changed")
                .order_by(RouteEvent.observed_at.desc(), RouteEvent.id.desc())
                .limit(1)
            )
            if switch is None:
                print("LAST_ROUTE_SWITCH=none")
            else:
                print(
                    "LAST_ROUTE_SWITCH="
                    f"{switch.observed_at.isoformat()}|{switch.previous_outbound or 'unknown'}|{switch.new_outbound or 'unknown'}"
                )
    finally:
        engine.dispose()
    return settings, factory


async def report_controller(settings: Settings) -> None:
    secret = settings.mihomo_api_secret.get_secret_value() if settings.mihomo_api_secret else None
    client = MihomoClient(str(settings.mihomo_url), secret=secret)
    try:
        groups = await client.groups()
    except Exception as error:
        print(f"FALLBACK_NOW=unavailable|TYPE={type(error).__name__}")
        return
    group = next((item for item in groups if item.name == "VPS-FALLBACK" and item.type and item.type.casefold() == "fallback"), None)
    if group is None or group.now not in {"WG-IMP", "HY2-USA"}:
        print("FALLBACK_NOW=unknown")
        return
    print(f"FALLBACK_NOW={group.now}")


settings, _factory = report_database()
asyncio.run(report_controller(settings))
PY
}

wireguard_snapshot() {
  output=$1
  "$DOCKER_BIN" exec "$WIREGUARD_CONTAINER" wg show wg0 dump 2>/dev/null | awk -F '\t' -v client="$CLIENT_ADDRESS" -v now="$NOW_EPOCH" '
    NR == 1 { next }
    NF < 7 { next }
    {
      split($4, addresses, ",")
      address = addresses[1]
      sub(/\/32$/, "", address)
      if (address != client) next
      age = $5 == 0 ? "never" : (now >= $5 ? now - $5 : 0)
      printf "%s|%s|%s|%s\n", address, age, $6, $7
      found = 1
      exit
    }
    END { if (!found) exit 1 }
  ' > "$output" || :
}

print_wireguard_snapshot() {
  file=$1
  prefix=$2
  if [ ! -s "$file" ]; then
    echo "${prefix}=unavailable"
    return
  fi
  IFS='|' read -r address age rx tx < "$file"
  echo "${prefix}=${address}|HANDSHAKE_AGE_SECONDS=${age}|SERVER_RX_BYTES=${rx}|SERVER_TX_BYTES=${tx}"
}

print_wireguard_delta() {
  before=$1
  after=$2
  if [ ! -s "$before" ] || [ ! -s "$after" ]; then
    echo 'WG_CLIENT_DELTA=unavailable'
    return
  fi
  IFS='|' read -r before_address before_age before_rx before_tx < "$before"
  IFS='|' read -r after_address after_age after_rx after_tx < "$after"
  [ "$before_address" = "$after_address" ] || { echo 'WG_CLIENT_DELTA=address_changed'; return; }
  rx_delta=$((after_rx - before_rx))
  tx_delta=$((after_tx - before_tx))
  [ "$rx_delta" -ge 0 ] || rx_delta=$after_rx
  [ "$tx_delta" -ge 0 ] || tx_delta=$after_tx
  handshake_changed=no
  [ "$before_age" = "$after_age" ] || handshake_changed=yes
  echo "WG_CLIENT_DELTA=HANDSHAKE_CHANGED=$handshake_changed|SERVER_RX_DELTA=$rx_delta|SERVER_TX_DELTA=$tx_delta"
}

echo 'RESULT=success'
echo 'VPN_INSTABILITY_DIAGNOSTIC=read_only'
echo "WATCH_SECONDS=$WATCH_SECONDS"
echo "CLIENT_ADDRESS=$CLIENT_ADDRESS"
echo 'CONTAINERS_BEFORE=begin'
container_state "$WIREGUARD_CONTAINER"
container_state vpn-mihomo
container_state "$DASHBOARD_CONTAINER"
echo 'CONTAINERS_BEFORE=end'
echo 'FALLBACK_CONFIG=begin'
fallback_config
echo 'FALLBACK_CONFIG=end'
echo 'DASHBOARD_OBSERVABILITY=begin'
if ! dashboard_observations; then
  echo 'DASHBOARD_OBSERVABILITY=unavailable'
fi
echo 'DASHBOARD_OBSERVABILITY=end'
wireguard_snapshot "$TMP/wg-before"
print_wireguard_snapshot "$TMP/wg-before" 'WG_CLIENT'
echo "WATCH_READY=generate normal traffic through $CLIENT_ADDRESS; seconds=$WATCH_SECONDS"
sleep "$WATCH_SECONDS"
wireguard_snapshot "$TMP/wg-after"
print_wireguard_delta "$TMP/wg-before" "$TMP/wg-after"
echo 'CONTAINERS_AFTER=begin'
container_state "$WIREGUARD_CONTAINER"
container_state vpn-mihomo
container_state "$DASHBOARD_CONTAINER"
echo 'CONTAINERS_AFTER=end'
