#!/bin/sh
# Read-only metadata; no sudo, module loading or firewall access.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /usr/bin/python3 -B "$SCRIPT_DIR/classify-antidpi-support.py"
