#!/bin/sh
# Comprehensive read-only health evidence for the Synology VPN gateway.
# It never prints WireGuard keys, controller secrets, endpoint addresses or config.
set -eu

DOCKER_BIN=${DOCKER_BIN:-/usr/local/bin/docker}
BTRFS_BIN=${BTRFS_BIN:-/sbin/btrfs}
VOLUME=${VOLUME:-/volume1}
NOW_EPOCH=${NOW_EPOCH:-}
WIREGUARD_CONTAINER=${WIREGUARD_CONTAINER:-vpn-wireguard}
DASHBOARD_CONTAINER=${DASHBOARD_CONTAINER:-vpn-dashboard}
EXPECTED_CONTAINERS="vpn-wireguard vpn-mihomo vpn-metacubexd vpn-uptime-kuma vpn-dashboard"

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -x "$DOCKER_BIN" ] || { echo "Docker binary is unavailable: $DOCKER_BIN" >&2; exit 1; }
[ -d "$VOLUME" ] || { echo "volume is unavailable: $VOLUME" >&2; exit 1; }
if [ -z "$NOW_EPOCH" ]; then
  NOW_EPOCH=$(date +%s)
fi
case "$NOW_EPOCH" in ''|*[!0-9]*) echo 'current time is unavailable' >&2; exit 1 ;; esac

TMP=$(mktemp -d /tmp/vpn-gateway-health.XXXXXX)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

container_state() {
  container=$1
  if "$DOCKER_BIN" inspect -f 'CONTAINER={{.Name}}|STATE={{.State.Status}}|RUNNING={{.State.Running}}|HEALTH={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|RESTARTS={{.RestartCount}}|OOM={{.State.OOMKilled}}|ERROR={{if .State.Error}}present{{else}}none{{end}}' "$container"; then
    :
  else
    echo "CONTAINER=/$container|STATE=missing|RUNNING=false|HEALTH=none|RESTARTS=unknown|OOM=unknown|ERROR=present"
  fi
}

echo 'RESULT=success'
echo 'GATEWAY_HEALTH_FORMAT=v1'

echo 'HOST=begin'
echo "HOSTNAME=$(hostname 2>/dev/null || echo unknown)"
echo "UPTIME=$(uptime 2>/dev/null || echo unavailable)"
echo "LOAD_AVERAGE=$(cut -d ' ' -f1-3 /proc/loadavg 2>/dev/null || echo unavailable)"
awk '
  /^MemTotal:/ { total=$2 }
  /^MemAvailable:/ { available=$2 }
  /^SwapTotal:/ { swap_total=$2 }
  /^SwapFree:/ { swap_free=$2 }
  END { printf "MEMORY_KIB=TOTAL=%s|AVAILABLE=%s|SWAP_TOTAL=%s|SWAP_FREE=%s\n", total, available, swap_total, swap_free }
' /proc/meminfo 2>/dev/null || echo 'MEMORY_KIB=unavailable'
df -Pk "$VOLUME" 2>/dev/null | awk 'NR == 2 { printf "VOLUME_KIB=TOTAL=%s|USED=%s|AVAILABLE=%s|CAPACITY=%s\n", $2, $3, $4, $5 }' || echo 'VOLUME_KIB=unavailable'
if [ -x "$BTRFS_BIN" ]; then
  "$BTRFS_BIN" scrub status "$VOLUME" 2>&1 | sed 's/^/BTRFS_SCRUB=/' || true
  "$BTRFS_BIN" device stats "$VOLUME" 2>&1 | sed 's/^/BTRFS_DEVICE_STAT=/' || true
else
  echo 'BTRFS=command_unavailable'
fi
kernel_alerts=$(dmesg 2>/dev/null | tail -n 500 | grep -Eic 'BTRFS.*(error|corrupt)|I/O error|oom-killer|Out of memory|segmentation fault' || true)
echo "KERNEL_HARD_ALERTS_LAST_500=$kernel_alerts"
echo 'HOST=end'

echo 'CONTAINERS=begin'
for container in $EXPECTED_CONTAINERS; do
  container_state "$container"
done
"$DOCKER_BIN" stats --no-stream --format 'RESOURCE={{.Name}}|CPU={{.CPUPerc}}|MEM={{.MemUsage}}|NET={{.NetIO}}|PIDS={{.PIDs}}' $EXPECTED_CONTAINERS 2>/dev/null || echo 'RESOURCE=unavailable'
for container in $EXPECTED_CONTAINERS; do
  hard_alerts=$("$DOCKER_BIN" logs --since 24h --tail 500 "$container" 2>&1 | grep -Eic 'fatal|panic|oom killed|segmentation fault' || true)
  echo "CONTAINER_HARD_ALERTS_24H=$container:$hard_alerts"
done
echo 'CONTAINERS=end'

echo 'WIREGUARD=begin'
if "$DOCKER_BIN" port "$WIREGUARD_CONTAINER" 51820/udp 2>/dev/null | grep -Eq ':[0-9]+$'; then
  echo 'UDP_51820=published'
else
  echo 'UDP_51820=not_published'
fi
forwarding=$("$DOCKER_BIN" exec "$WIREGUARD_CONTAINER" sh -c 'cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || true' 2>/dev/null | tr -d '\r\n')
case "$forwarding" in 1) echo 'IPV4_FORWARD=enabled' ;; *) echo 'IPV4_FORWARD=unknown_or_disabled' ;; esac
if "$DOCKER_BIN" exec "$WIREGUARD_CONTAINER" wg show wg0 dump > "$TMP/wg.dump" 2>/dev/null; then
  awk -F '\t' -v now="$NOW_EPOCH" '
    NR == 1 { next }
    $5 ~ /^[0-9]+$/ && $6 ~ /^[0-9]+$/ && $7 ~ /^[0-9]+$/ {
      total += 1
      if ($5 > 0 && now >= $5 && now - $5 <= 300) fresh += 1
      if ($6 > 0 || $7 > 0) traffic += 1
    }
    END { printf "WG_PEERS_TOTAL=%d\nWG_PEERS_FRESH_5_MINUTES=%d\nWG_PEERS_WITH_TRAFFIC=%d\n", total, fresh, traffic }
  ' "$TMP/wg.dump"
else
  echo 'WG_PEERS=unavailable'
fi
echo 'WIREGUARD=end'

echo 'MIHOMO=begin'
if "$DOCKER_BIN" exec -i "$DASHBOARD_CONTAINER" python - <<'PY'
import asyncio

from app.mihomo import MihomoClient
from app.settings import Settings


async def main() -> None:
    try:
        settings = Settings.from_env()
        secret = settings.mihomo_api_secret.get_secret_value() if settings.mihomo_api_secret else None
        client = MihomoClient(str(settings.mihomo_url), secret=secret)
        await client.version()
        groups = await client.groups()
        providers = await client.rule_providers()
    except Exception as error:
        print(f"MIHOMO_CONTROLLER=unavailable|TYPE={type(error).__name__}")
    else:
        print(f"MIHOMO_CONTROLLER=ok|GROUPS={len(groups)}|RULE_PROVIDERS={len(providers)}")


asyncio.run(main())
PY
then
  :
else
  echo 'MIHOMO_CONTROLLER=unavailable|TYPE=dashboard_exec_failed'
fi
echo 'MIHOMO=end'
