from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class UnsafeUrlError(ValueError):
    pass


_TRACKING_KEYS = {"fbclid", "gclid", "igshid", "si"}


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return bool(address.is_global)


def validate_public_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 4096:
        raise UnsafeUrlError("invalid URL length")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("invalid URL") from exc
    if parts.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrlError("only HTTP(S) URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise UnsafeUrlError("credentials in URL are not allowed")
    hostname = (parts.hostname or "").rstrip(".").lower()
    if not hostname or hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeUrlError("local host is not allowed")
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeUrlError("invalid port")

    try:
        literal = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise UnsafeUrlError("non-public IP is not allowed")
        return url

    try:
        ascii_host = hostname.encode("idna").decode("ascii")
        records = socket.getaddrinfo(
            ascii_host, port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM
        )
    except (UnicodeError, socket.gaierror, OSError) as exc:
        raise UnsafeUrlError("host cannot be resolved") from exc
    addresses = {record[4][0] for record in records if record and record[4]}
    if not addresses or not all(_is_public_ip(address) for address in addresses):
        raise UnsafeUrlError("host resolves to a non-public IP")
    return url


def normalized_url_key(url: str) -> str:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").rstrip(".").lower().encode("idna").decode("ascii")
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_KEYS and not key.lower().startswith("utm_")
    ]
    return urlunsplit((scheme, host, path, urlencode(sorted(query)), ""))


def safe_url_for_log(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or "invalid"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))[:512]
    except Exception:
        return "<invalid-url>"
