from __future__ import annotations

import socket

import pytest

from app.url_security import UnsafeUrlError, normalized_url_key, safe_url_for_log, validate_public_url


def _dns(address: str):
    return [(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/video",
        "http://10.0.0.1/video",
        "http://172.16.0.1/video",
        "http://192.168.1.1/video",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/video",
        "http://[fc00::1]/video",
        "file:///etc/passwd",
        "http://user:pass@example.com/video",
        "http://localhost/video",
    ],
)
def test_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        validate_public_url(url)


def test_rejects_hostname_with_private_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: _dns("192.168.1.10"))
    with pytest.raises(UnsafeUrlError):
        validate_public_url("https://example.com/video")


def test_accepts_public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: _dns("93.184.216.34"))
    assert validate_public_url("https://example.com/video") == "https://example.com/video"


def test_normalization_removes_tracking_and_fragment() -> None:
    left = normalized_url_key("HTTPS://Example.COM:443/video?utm_source=x&id=2&si=abc#part")
    right = normalized_url_key("https://example.com/video?id=2")
    assert left == right


def test_log_url_hides_query() -> None:
    assert safe_url_for_log("https://example.com/video?token=secret") == "https://example.com/video"
