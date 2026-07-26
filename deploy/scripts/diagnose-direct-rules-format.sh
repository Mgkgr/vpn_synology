#!/bin/sh
# Read only: classify the current DIRECT provider without disclosing domains.
set -eu

DIRECT_FILE=${DIRECT_FILE:-/volume1/docker/vpn-gateway/mihomo/rules/direct.txt}

[ "$(id -u)" = 0 ] || { echo 'must run as root' >&2; exit 1; }
[ -f "$DIRECT_FILE" ] || { echo "DIRECT provider is missing: $DIRECT_FILE" >&2; exit 1; }
[ -x /usr/bin/python3 ] || { echo 'python3 is unavailable' >&2; exit 1; }

/usr/bin/python3 - "$DIRECT_FILE" <<'PY'
from __future__ import annotations

from collections import Counter
from pathlib import Path
import sys

path = Path(sys.argv[1])
allowed = {"DOMAIN", "DOMAIN-SUFFIX", "DOMAIN-KEYWORD", "IP-CIDR", "GEOIP", "GEOSITE"}
counts: Counter[str] = Counter()
first_problem: tuple[int, str] | None = None

for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
    line = raw.strip()
    if not line:
        counts["blank"] += 1
        continue
    if line.startswith("#"):
        counts["comment"] += 1
        continue
    parts = [part.strip() for part in line.split(",")]
    rule_type = parts[0] if parts else ""
    if len(parts) == 3 and rule_type in allowed and parts[2] == "DIRECT":
        kind = "terminal_direct"
    elif len(parts) == 2 and rule_type in {"DOMAIN-SUFFIX", "IP-CIDR"}:
        kind = "implicit_direct"
    elif len(parts) == 4 and rule_type == "IP-CIDR" and parts[2] == "DIRECT" and parts[3] == "no-resolve":
        kind = "ip_cidr_direct_no_resolve"
    else:
        kind = "unsupported"
    counts[kind] += 1
    if kind not in {"terminal_direct", "blank", "comment"} and first_problem is None:
        first_problem = (line_number, kind)

print("RESULT=success")
print("DIRECT_FORMAT_DIAGNOSTIC=read_only")
for name in ("terminal_direct", "implicit_direct", "ip_cidr_direct_no_resolve", "unsupported", "comment", "blank"):
    print(f"FORMAT_{name.upper()}={counts[name]}")
if first_problem is not None:
    print(f"FIRST_NONTERMINAL_LINE={first_problem[0]}|FORMAT={first_problem[1]}")
else:
    print("FIRST_NONTERMINAL_LINE=none")
PY
