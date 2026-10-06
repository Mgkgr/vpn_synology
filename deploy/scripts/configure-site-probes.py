"""Pure transform for independent daily HEAD wrappers. No file/network writes.

Caller must provide backup/CAS, Mihomo validation and an approved reload window.
Reuses the strict block-list parser from the existing health-probe transform.
"""

import importlib.util
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "dashboard_health_probe_transform", Path(__file__).with_name("configure-health-probes.py")
)
_PARSER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_PARSER)

BEGIN = "  # BEGIN dashboard isolated site probes v1\n"
END = "  # END dashboard isolated site probes v1\n"
MEMBERS = ("DIRECT", "WG-IMP", "HY2-USA")


def _block():
    return BEGIN + "".join(
        "  - name: DASH-SITE-{0}\n"
        "    type: select\n"
        "    hidden: true\n"
        "    interval: 0\n"
        "    empty-fallback: REJECT\n"
        "    proxies:\n"
        "      - {0}\n".format(member)
        for member in MEMBERS
    ) + END


def configure_site_probes(text: str) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    source = text.replace("\r\n", "\n")
    if "\r" in source or "\t" in source or (newline == "\r\n" and "\n" in text.replace("\r\n", "")):
        raise ValueError("mixed line endings or tabs are not supported")
    if BEGIN in source or END in source:
        if source.count(BEGIN) != 1 or source.count(END) != 1:
            raise ValueError("invalid site probe markers")
        start, end = source.index(BEGIN), source.index(END) + len(END)
        section_start, section_end = _PARSER._section(source, "proxy-groups")
        if not section_start <= start < end <= section_end or source[start:end] != _block():
            raise ValueError("site probe block modified or outside proxy groups")
        source = source[:start] + source[end:]
    if "DASH-SITE-" in source:
        raise ValueError("site probe name already used or referenced")
    proxies = _PARSER._names(source, "proxies")
    groups = _PARSER._names(source, "proxy-groups")
    if any(member not in proxies for member in MEMBERS[1:]) or len(set(proxies + groups)) != len(proxies + groups):
        raise ValueError("missing explicit outbound or duplicate name")
    if "DIRECT" in proxies + groups or "REJECT" in proxies + groups:
        raise ValueError("built-in outbound name is shadowed")
    start, _ = _PARSER._section(source, "proxy-groups")
    return (source[:start] + _block() + source[start:]).replace("\n", newline)
