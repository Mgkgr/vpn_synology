#!/bin/sh
set -eu

JOB=${1:?missing job path}
IMPORTER=${2:?missing importer path}
NORMALIZER=${3:?missing normalizer path}
SOURCE=${4:?missing import source path}
STATUS_FILE=${5:?missing public status path}
PRIVATE_LOG=${6:?missing private log path}

umask 077
nohup /bin/sh "$JOB" "$IMPORTER" "$NORMALIZER" "$SOURCE" "$STATUS_FILE" "$PRIVATE_LOG" >/dev/null 2>&1 &
printf '%s\n' "$!" > "${PRIVATE_LOG}.pid"
rm -f "$0"
