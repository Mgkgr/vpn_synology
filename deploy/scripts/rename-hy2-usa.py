#!/usr/bin/env python3
"""Safely migrate the single reserve Hysteria2 exit to HY2-USA.

The script deliberately uses narrow, validated textual anchors instead of a
generic YAML reformatter: the gateway config contains comments and provider
ordering that must remain byte-for-byte stable outside this one migration.
It never prints values from the profile file.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path


LEGACY_NAME = "HY2-NL"
CURRENT_NAME = "HY2-USA"
LEGACY_PROVIDER = "managed-hy2-nl"
CURRENT_PROVIDER = "managed-hy2-usa"
_PROFILE_KEYS = (
    "HY2_SERVER",
    "HY2_PORT",
    "HY2_PORTS",
    "HY2_PASSWORD",
    "HY2_OBFS_PASSWORD",
    "HY2_SNI",
)
_PROFILE_LINE = re.compile(r"([A-Z0-9_]+)='([^'\r\n]*)'\Z")
_HOSTNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z")
_SECTION = re.compile(r"(?m)^[A-Za-z][A-Za-z0-9_-]*:\s*$")


class MigrationError(ValueError):
    """Raised for unexpected source files; callers perform rollback."""


def _profile(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise MigrationError("reserve profile is unreadable") from error
    for raw in lines:
        if not raw:
            continue
        match = _PROFILE_LINE.fullmatch(raw)
        if match is None:
            raise MigrationError("reserve profile has an invalid line")
        key, value = match.groups()
        if key not in _PROFILE_KEYS or key in values or not value:
            raise MigrationError("reserve profile has invalid fields")
        values[key] = value
    if tuple(values) != _PROFILE_KEYS:
        raise MigrationError("reserve profile fields are incomplete or out of order")
    for key in ("HY2_SERVER", "HY2_SNI"):
        if not _HOSTNAME.fullmatch(values[key]):
            raise MigrationError("reserve profile hostname is invalid")
    try:
        port = int(values["HY2_PORT"])
        first, last = (int(part) for part in values["HY2_PORTS"].split("-", 1))
    except ValueError as error:
        raise MigrationError("reserve profile ports are invalid") from error
    if not 1 <= port <= 65535 or not 1 <= first <= last <= 65535:
        raise MigrationError("reserve profile ports are outside the valid range")
    return values


def _yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _entry_block(text: str, name: str) -> tuple[int, int, str]:
    marker = f"  - name: {name}\n"
    positions = [match.start() for match in re.finditer(re.escape(marker), text)]
    if len(positions) != 1:
        raise MigrationError(f"expected exactly one {name} entry")
    start = positions[0]
    next_entry = text.find("\n  - name: ", start + len(marker))
    next_section = _SECTION.search(text, start + len(marker))
    candidates = [position for position in (next_entry + 1 if next_entry >= 0 else -1, next_section.start() if next_section else -1, len(text)) if position >= 0]
    end = min(candidates)
    return start, end, text[start:end]


def _fallback_block(text: str) -> tuple[int, int, str]:
    start, end, block = _entry_block(text, "VPS-FALLBACK")
    if "    type: fallback\n" not in block:
        raise MigrationError("VPS-FALLBACK is not a fallback group")
    return start, end, block


def _replace_once(text: str, old: str, new: str, description: str) -> str:
    if text.count(old) != 1:
        raise MigrationError(f"expected exactly one {description}")
    return text.replace(old, new, 1)


def _proxy_block(values: dict[str, str]) -> str:
    return """  - name: HY2-USA
    type: hysteria2
    server: {server}
    port: {port}
    ports: {ports}
    password: {password}
    obfs: salamander
    obfs-password: {obfs_password}
    sni: {sni}
    skip-cert-verify: false
    udp: true
""".format(
        server=_yaml(values["HY2_SERVER"]),
        port=values["HY2_PORT"],
        ports=_yaml(values["HY2_PORTS"]),
        password=_yaml(values["HY2_PASSWORD"]),
        obfs_password=_yaml(values["HY2_OBFS_PASSWORD"]),
        sni=_yaml(values["HY2_SNI"]),
    )


def _write_atomic(path: Path, content: str) -> None:
    metadata = path.stat()
    descriptor, temporary_name = tempfile.mkstemp(prefix=".hy2-usa.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if hasattr(os, "chown"):
            os.chown(temporary, metadata.st_uid, metadata.st_gid)
        os.chmod(temporary, stat.S_IMODE(metadata.st_mode))
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def migrate(profile_path: Path, config_path: Path, rules_dir: Path) -> None:
    values = _profile(profile_path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as error:
        raise MigrationError("Mihomo configuration is unreadable") from error

    legacy_proxy = f"  - name: {LEGACY_NAME}\n" in text
    current_proxy = f"  - name: {CURRENT_NAME}\n" in text
    if legacy_proxy == current_proxy:
        raise MigrationError("reserve proxy migration state is ambiguous")

    if legacy_proxy:
        start, end, block = _entry_block(text, LEGACY_NAME)
        if "    type: hysteria2\n" not in block:
            raise MigrationError("HY2-NL is not a Hysteria2 proxy")
        text = text[:start] + _proxy_block(values) + text[end:]

        start, end, fallback = _fallback_block(text)
        fallback = _replace_once(fallback, f"      - {LEGACY_NAME}\n", f"      - {CURRENT_NAME}\n", "legacy fallback member")
        if f"      - {CURRENT_NAME}\n" not in fallback:
            raise MigrationError("HY2-USA is missing from fallback")
        text = text[:start] + fallback + text[end:]
        text = _replace_once(text, f"  {LEGACY_PROVIDER}:\n", f"  {CURRENT_PROVIDER}:\n", "legacy rule provider")
        text = _replace_once(
            text,
            f"    path: ./rules/{LEGACY_PROVIDER}.txt\n",
            f"    path: ./rules/{CURRENT_PROVIDER}.txt\n",
            "legacy provider path",
        )
        text = _replace_once(
            text,
            f"  - RULE-SET,{LEGACY_PROVIDER},{LEGACY_NAME}\n",
            f"  - RULE-SET,{CURRENT_PROVIDER},{CURRENT_NAME}\n",
            "legacy managed rule",
        )
    else:
        start, end, block = _entry_block(text, CURRENT_NAME)
        if "    type: hysteria2\n" not in block:
            raise MigrationError("HY2-USA is not a Hysteria2 proxy")
        text = text[:start] + _proxy_block(values) + text[end:]
        _start, _end, fallback = _fallback_block(text)
        if fallback.count(f"      - {CURRENT_NAME}\n") != 1 or f"      - {LEGACY_NAME}\n" in fallback:
            raise MigrationError("current fallback member is invalid")
        for value, description in (
            (f"  {CURRENT_PROVIDER}:\n", "current rule provider"),
            (f"    path: ./rules/{CURRENT_PROVIDER}.txt\n", "current provider path"),
            (f"  - RULE-SET,{CURRENT_PROVIDER},{CURRENT_NAME}\n", "current managed rule"),
        ):
            if text.count(value) != 1:
                raise MigrationError(f"expected exactly one {description}")

    if LEGACY_NAME in text or LEGACY_PROVIDER in text:
        raise MigrationError("a legacy HY2 reference remains in configuration")

    legacy_rule = rules_dir / f"{LEGACY_PROVIDER}.txt"
    current_rule = rules_dir / f"{CURRENT_PROVIDER}.txt"
    if legacy_rule.exists() and current_rule.exists():
        raise MigrationError("both legacy and current HY2 rule files exist")
    if legacy_rule.exists():
        os.replace(legacy_rule, current_rule)
    elif not current_rule.is_file():
        raise MigrationError("HY2 managed rule file is missing")

    _write_atomic(config_path, text)


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: rename-hy2-usa.py PROFILE CONFIG RULES_DIR", file=sys.stderr)
        return 64
    try:
        migrate(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
    except (MigrationError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
