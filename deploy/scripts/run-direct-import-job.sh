#!/bin/sh
# Run a direct-rule import independently from the SSH session that starts it.
# A Mihomo reload can reset that session although the import has completed.
set -eu

IMPORTER=${1:?missing importer path}
NORMALIZER=${2:?missing normalizer path}
SOURCE=${3:?missing import source path}
STATUS_FILE=${4:?missing public status path}
PRIVATE_LOG=${5:?missing private log path}

umask 077
: > "$PRIVATE_LOG"
chmod 0600 "$PRIVATE_LOG"

cleanup() {
  rm -f "$IMPORTER" "$NORMALIZER" "$SOURCE"
  case "$0" in
    /tmp/vpn-dashboard-direct-job-*.sh) rm -f "$0" ;;
  esac
}

write_status() {
  result=${1:?missing result}
  temporary="${STATUS_FILE}.tmp.$$"
  {
    printf 'RESULT=%s\n' "$result"
    if [ "$result" = success ]; then
      grep -E '^(DIRECT_RULES_CURRENT|DIRECT_IMPORT_RULES|DIRECT_IMPORT_ALREADY_PRESENT|DIRECT_IMPORT_ADDED|DIRECT_RULES_REVISION|MIHOMO_RELOAD|ENCRYPTED_BACKUP)=' "$PRIVATE_LOG" || true
    fi
  } > "$temporary"
  chmod 0644 "$temporary"
  mv -f "$temporary" "$STATUS_FILE"
}

if /bin/sh "$IMPORTER" "$NORMALIZER" "$SOURCE" > "$PRIVATE_LOG" 2>&1; then
  write_status success
  cleanup
  exit 0
fi

write_status failed
cleanup
exit 1
