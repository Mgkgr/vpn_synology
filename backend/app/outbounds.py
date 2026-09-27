"""Stable routing IDs; provider labels are not verified exit geolocation."""

from dataclasses import dataclass
from typing import Literal


OutboundId = Literal["WG-IMP", "HY2-USA", "ANTIDPI"]
FallbackOutboundId = Literal["WG-IMP", "HY2-USA"]
AntidpiEngine = Literal["zapret2", "byedpi"]
FALLBACK_MEMBERS: tuple[FallbackOutboundId, ...] = ("WG-IMP", "HY2-USA")


@dataclass(frozen=True)
class OutboundDefinition:
    id: OutboundId
    label: str
    engine: str
    probe_name: str


def build_outbound_registry(antidpi_engine: AntidpiEngine | None = None) -> tuple[OutboundDefinition, ...]:
    entries = (
        OutboundDefinition("WG-IMP", "VLESS-NL", "vless", "DASH-HEALTH-WG-IMP"),
        OutboundDefinition("HY2-USA", "HY2-DE", "hysteria2", "DASH-HEALTH-HY2-USA"),
    )
    if antidpi_engine is None:
        return entries
    if antidpi_engine not in ("zapret2", "byedpi"):
        raise ValueError("unsupported anti-DPI engine")
    return entries + (OutboundDefinition("ANTIDPI", "ANTIDPI", antidpi_engine, "DASH-HEALTH-ANTIDPI"),)
