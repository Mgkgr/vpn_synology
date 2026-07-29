#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
script="$project_root/deploy/scripts/mihomo-boot-autostart.sh"

[ -f "$script" ] || {
  echo "missing boot autostart script: $script" >&2
  exit 1
}

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
state="$tmp/state"
log="$tmp/docker.log"
mkdir -p "$state"

cat > "$tmp/docker" <<'EOF'
#!/bin/sh
set -eu

state=${FAKE_DOCKER_STATE:?}
log=${FAKE_DOCKER_LOG:?}

case "$1" in
  inspect)
    if [ "${2:-}" != "-f" ]; then
      exit 0
    fi
    format=$3
    container=$4
    case "$format:$container" in
      '{{.State.Running}}:vpn-wireguard') [ -f "$state/wireguard.running" ] && echo true || echo false ;;
      '{{.State.Running}}:vpn-mihomo') [ -f "$state/mihomo.running" ] && echo true || echo false ;;
      '{{.Id}}:vpn-wireguard') echo 'wireguard-test-id' ;;
      '{{.HostConfig.NetworkMode}}:vpn-mihomo') echo 'container:vpn-wireguard' ;;
      *) echo "unexpected inspect: $format:$container" >&2; exit 1 ;;
    esac
    ;;
  start)
    echo "start:$2" >> "$log"
    case "$2" in
      vpn-wireguard) : > "$state/wireguard.running" ;;
      vpn-mihomo) : > "$state/mihomo.running" ;;
      *) echo "unexpected start: $2" >&2; exit 1 ;;
    esac
    ;;
  *)
    echo "unexpected docker command: $*" >&2
    exit 1
    ;;
esac
EOF
chmod 700 "$tmp/docker"

FAKE_DOCKER_STATE="$state" \
FAKE_DOCKER_LOG="$log" \
DOCKER_BIN="$tmp/docker" \
RETRY_DELAY_SECONDS=1 \
MAX_WAIT_SECONDS=2 \
LOG_FILE="$tmp/boot.log" \
/bin/sh "$script"

[ "$(cat "$log")" = "start:vpn-wireguard
start:vpn-mihomo" ] || {
  echo 'expected WireGuard and then Mihomo to start' >&2
  cat "$log" >&2
  exit 1
}

grep -qx 'MIHOMO_AUTOSTART=success' "$tmp/boot.log"
echo 'PASS: Mihomo waits for WireGuard at boot'
