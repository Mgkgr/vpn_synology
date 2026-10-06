"""Fixed public HEAD targets; not an editable URL scanner or routing policy."""

from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class SiteProbeTarget:
    key: str
    category: str
    url: str


@dataclass(frozen=True, slots=True)
class SiteProbeRoute:
    group: str
    member: str
    label: str


@dataclass(frozen=True, slots=True)
class SiteProbeObservation:
    state: str
    delay_ms: int | None = None
    reason: str | None = None


SITE_PROBE_ROUTES = MappingProxyType({
    "direct": SiteProbeRoute("DASH-SITE-DIRECT", "DIRECT", "DIRECT"),
    "primary": SiteProbeRoute("DASH-SITE-WG-IMP", "WG-IMP", "Основной"),
    "reserve": SiteProbeRoute("DASH-SITE-HY2-USA", "HY2-USA", "Резервный"),
})

# Reviewed against official service entry points, 2026-10-04. Some restrict
# automated requests; their response is not proof of application availability.
_URLS = {
    "openai": "https://chatgpt.com/",
    "anthropic": "https://claude.ai/",
    "google": "https://www.google.com/generate_204",
    "github": "https://api.github.com/",
    "youtube": "https://www.youtube.com/generate_204",
    "telegram": "https://telegram.org/",
    "microsoft": "https://www.microsoft.com/",
    "apple": "https://www.apple.com/",
    "netflix": "https://www.netflix.com/",
    "spotify": "https://open.spotify.com/",
    "twitter": "https://x.com/",
    "tiktok": "https://www.tiktok.com/",
    "disney": "https://www.disneyplus.com/",
    "hbo": "https://www.hbomax.com/",
    "primevideo": "https://www.primevideo.com/",
    "twitch": "https://www.twitch.tv/",
    "dazn": "https://www.dazn.com/",
    "bilibili": "https://www.bilibili.com/",
    "biliintl": "https://www.bilibili.tv/",
    "steam": "https://store.steampowered.com/",
    "ehentai": "https://e-hentai.org/",
    "speedtest": "https://www.speedtest.net/",
    "kinopoisk": "https://www.kinopoisk.ru/",
    "ozon": "https://www.ozon.ru/",
    "wildberries": "https://www.wildberries.ru/",
    "avito": "https://www.avito.ru/",
    "sber": "https://www.sberbank.ru/",
    "tbank-ru": "https://www.tbank.ru/",
    "yandex": "https://ya.ru/",
    "vk": "https://vk.com/",
    "mailru-group": "https://mail.ru/",
    "cdek": "https://www.cdek.ru/",
    "megafon": "https://www.megafon.ru/",
    "mts-ru": "https://www.mts.ru/",
    "rostelecom": "https://www.rt.ru/",
    "t2-ru": "https://t2.ru/",
}


def _target(key: str, url: str) -> SiteProbeTarget:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or "." not in parsed.hostname
            or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("invalid built-in service probe target")
    return SiteProbeTarget(key, key, url)


SITE_PROBE_TARGETS = MappingProxyType({key: _target(key, url) for key, url in _URLS.items()})
