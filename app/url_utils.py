from __future__ import annotations

from urllib.parse import urlparse


def _host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").rstrip(".").lower()
        return host.removeprefix("www.")
    except (TypeError, ValueError):
        return ""


def is_youtube_url(url: str) -> bool:
    host = _host(url)
    if not host:
        return False
    domains = ("youtube.com", "youtu.be", "m.youtube.com")
    return any(host == d or host.endswith("." + d) for d in domains)


def is_instagram_url(url: str) -> bool:
    host = _host(url)
    if not host:
        return False
    domains = ("instagram.com", "instagr.am")
    return any(host == d or host.endswith("." + d) for d in domains)
