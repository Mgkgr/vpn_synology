"""Pure, deliberately narrow config transform; caller owns backup/CAS/validation.

Python 3.8 compatible, no YAML dependency on the NAS. Only the existing block-list
layout is supported. Unexpected structure fails without rewriting any input.
"""

from __future__ import annotations

import re


BEGIN = "  # BEGIN dashboard isolated health probes v1\n"
END = "  # END dashboard isolated health probes v1\n"
IDS = ("WG-IMP", "HY2-USA", "ANTIDPI")
_TOP = re.compile(r"(?m)^([A-Za-z][A-Za-z0-9_-]*):[^\n]*$")
_ENTRY = re.compile(r"^  - name: ([A-Za-z0-9_-]+)\s*(?:#.*)?$")


class HealthProbeConfigError(ValueError):
    """Safe fixed-message error; never includes source configuration or secrets."""


def _block(ids):
    return BEGIN + "".join(
        "  - name: DASH-HEALTH-{0}\n"
        "    type: select\n"
        "    hidden: true\n"
        "    interval: 0\n"
        "    empty-fallback: REJECT\n"
        "    proxies:\n"
        "      - {0}\n".format(name)
        for name in ids
    ) + END


def _section(text, key):
    roots = list(_TOP.finditer(text))
    matches = [i for i, item in enumerate(roots) if item.group(1) == key]
    if len(matches) != 1:
        raise HealthProbeConfigError("expected a single supported section")
    index = matches[0]
    root = roots[index]
    if not re.fullmatch(re.escape(key) + r":\s*(?:#.*)?", root.group()):
        raise HealthProbeConfigError("inline sections are not supported")
    start = root.end() + 1
    end = roots[index + 1].start() if index + 1 < len(roots) else len(text)
    return start, end


def _names(text, key):
    start, end = _section(text, key)
    names = []
    for line in text[start:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        entry = _ENTRY.fullmatch(line)
        if entry:
            names.append(entry.group(1))
        elif not names or not line.startswith("    ") or line.startswith("    name:"):
            raise HealthProbeConfigError("unsupported entry layout")
    if not names or len(set(names)) != len(names):
        raise HealthProbeConfigError("missing or duplicate entries")
    return names


def configure_health_probes(text: str, outbound_ids: tuple) -> str:
    if not outbound_ids or len(set(outbound_ids)) != len(outbound_ids) or any(name not in IDS for name in outbound_ids):
        raise HealthProbeConfigError("invalid health registry")
    newline = "\r\n" if "\r\n" in text else "\n"
    source = text.replace("\r\n", "\n")
    if "\r" in source or (newline == "\r\n" and "\n" in text.replace("\r\n", "")) or "\t" in source:
        raise HealthProbeConfigError("mixed line endings or tabs are not supported")
    if BEGIN in source or END in source:
        if source.count(BEGIN) != 1 or source.count(END) != 1:
            raise HealthProbeConfigError("invalid managed markers")
        start, end = source.index(BEGIN), source.index(END) + len(END)
        section_start, section_end = _section(source, "proxy-groups")
        if not section_start <= start < end <= section_end:
            raise HealthProbeConfigError("managed block outside proxy groups")
        owned = source[start:end]
        old_ids = tuple(re.findall(r"(?m)^  - name: DASH-HEALTH-([A-Z0-9-]+)$", owned))
        if not old_ids or len(set(old_ids)) != len(old_ids) or any(name not in IDS for name in old_ids) or _block(old_ids) != owned:
            raise HealthProbeConfigError("managed block was modified; manual review required")
        source = source[:start] + source[end:]
    proxies = _names(source, "proxies")
    groups = _names(source, "proxy-groups")
    all_names = proxies + groups
    if len(all_names) != len(set(all_names)) or any(name.startswith("DASH-HEALTH-") for name in all_names):
        raise HealthProbeConfigError("health probe name is already occupied")
    if any(name not in proxies for name in outbound_ids):
        raise HealthProbeConfigError("required explicit outbound is missing")
    start, _ = _section(source, "proxy-groups")
    return (source[:start] + _block(outbound_ids) + source[start:]).replace("\n", newline)
