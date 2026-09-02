#!/bin/sh
# Start the privileged migration without nested shell quoting.  All arguments
# are generated /tmp paths, never Hysteria2 credentials.
set -eu

APPLIER=${1:?missing applier}
PROFILE=${2:?missing profile}
RENAMER=${3:?missing renamer}
STATUS_FILE=${4:?missing status file}
PRIVATE_LOG=${5:?missing private log file}

umask 077
nohup /bin/sh "$APPLIER" "$PROFILE" "$RENAMER" "$STATUS_FILE" "$PRIVATE_LOG" > "$PRIVATE_LOG" 2>&1 &
printf '%s\n' "$!" > "${PRIVATE_LOG}.pid"
rm -f "$0"
