#!/usr/bin/env python3
import sys
from runtime import probe_socks, read_secret

try:
    if sys.argv[1:] == ['engine']:
        healthy = probe_socks(1081)
    elif sys.argv[1:] == ['socks']:
        healthy = probe_socks(1081) and probe_socks(1080, read_secret())
    else:
        healthy = False
except Exception:
    healthy = False
sys.exit(0 if healthy else 1)
