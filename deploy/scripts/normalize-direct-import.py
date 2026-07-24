from __future__ import annotations

import argparse
import ipaddress
import os
import re
import sys
import tempfile
from pathlib import Path


_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_GLUED_RULE = re.compile(r"(?<=[A-Za-z0-9])(?=(?:DOMAIN-SUFFIX|IP-CIDR),)")


class DirectImportError(ValueError):
    pass


def normalize(text: str) -> tuple[tuple[str, ...], int]:
    candidates = _GLUED_RULE.sub("\n", text).splitlines()
    normalized: list[str] = []
    seen: set[str] = set()
    duplicates = 0
    for line_number, raw in enumerate(candidates, start=1):
        line = raw.strip()
        if not line:
            continue
        rule = _normalize_line(line, line_number)
        if rule in seen:
            duplicates += 1
            continue
        seen.add(rule)
        normalized.append(rule)
    if not normalized:
        raise DirectImportError("the import does not contain any rules")
    return tuple(normalized), duplicates


def _normalize_line(line: str, line_number: int) -> str:
    parts = [part.strip() for part in line.split(",")]
    rule_type = parts[0] if parts else ""
    if rule_type == "DOMAIN-SUFFIX" and len(parts) in {2, 3}:
        if len(parts) == 3 and parts[2].upper() != "DIRECT":
            raise DirectImportError(f"line {line_number} is not an importable DIRECT rule")
        return f"DOMAIN-SUFFIX,{_validated_domain(parts[1], line_number)},DIRECT"
    if rule_type == "IP-CIDR" and len(parts) in {2, 3, 4}:
        if len(parts) >= 3 and parts[2].upper() != "DIRECT":
            raise DirectImportError(f"line {line_number} is not an importable DIRECT rule")
        try:
            network = ipaddress.ip_network(parts[1], strict=False)
        except ValueError as error:
            raise DirectImportError(f"line {line_number} has an invalid IP network") from error
        if network.version != 4:
            raise DirectImportError(f"line {line_number} has an invalid IPv4 network")
        return f"IP-CIDR,{network},DIRECT"
    raise DirectImportError(f"line {line_number} is not an importable DIRECT rule")


def _validated_domain(value: str, line_number: int) -> str:
    try:
        domain = value.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise DirectImportError(f"line {line_number} has an invalid domain") from error
    if len(domain) > 253 or domain.endswith(".") or not domain or any(
        not _DOMAIN_LABEL.fullmatch(label) for label in domain.split(".")
    ):
        raise DirectImportError(f"line {line_number} has an invalid domain")
    return domain


def _atomic_write(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".direct-import.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize a limited DIRECT-rule import without changing its source.")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    try:
        rules, duplicates = normalize(arguments.source.read_text(encoding="utf-8"))
        _atomic_write(arguments.output, "\n".join(rules) + "\n")
    except (DirectImportError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(f"NORMALIZED_RULES={len(rules)}")
    print(f"DUPLICATE_INPUT_RULES={duplicates}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
