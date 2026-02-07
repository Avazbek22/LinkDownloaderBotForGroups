from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _env_str(name: str, default: str) -> str:
    raw = (os.getenv(name) or "").strip()
    return raw or default


YTDLP_JS_RUNTIMES = _env_str("YTDLP_JS_RUNTIMES", "node")
YTDLP_REMOTE_COMPONENTS = _env_str("YTDLP_REMOTE_COMPONENTS", "ejs:github")

YTDLP_INSTAGRAM_IMPERSONATE = _env_str("YTDLP_INSTAGRAM_IMPERSONATE", "chrome")
YTDLP_INSTAGRAM_RETRIES = _env_int("YTDLP_INSTAGRAM_RETRIES", 8)
YTDLP_INSTAGRAM_FRAGMENT_RETRIES = _env_int("YTDLP_INSTAGRAM_FRAGMENT_RETRIES", 8)
YTDLP_INSTAGRAM_SOCKET_TIMEOUT = _env_int("YTDLP_INSTAGRAM_SOCKET_TIMEOUT", 30)
