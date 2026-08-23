from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be positive")
    return value


def _env_str(name: str, default: str, *, allow_empty: bool = False) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip()
    return value if value or allow_empty else default


def reload_from_environment() -> None:
    """Refresh yt-dlp settings after the local .env file has been loaded."""
    global YTDLP_JS_RUNTIMES
    global YTDLP_REMOTE_COMPONENTS
    global YTDLP_INSTAGRAM_IMPERSONATE
    global YTDLP_INSTAGRAM_RETRIES
    global YTDLP_INSTAGRAM_FRAGMENT_RETRIES
    global YTDLP_INSTAGRAM_SOCKET_TIMEOUT
    global YTDLP_YOUTUBE_PLAYER_CLIENT
    global YTDLP_YOUTUBE_PLAYER_CLIENTS

    YTDLP_JS_RUNTIMES = _env_str("YTDLP_JS_RUNTIMES", "node")
    # Unlike an unset variable, an explicitly empty value disables remote
    # components. This keeps the default convenient without making it mandatory.
    YTDLP_REMOTE_COMPONENTS = _env_str("YTDLP_REMOTE_COMPONENTS", "ejs:github", allow_empty=True)

    YTDLP_INSTAGRAM_IMPERSONATE = _env_str("YTDLP_INSTAGRAM_IMPERSONATE", "chrome")
    YTDLP_INSTAGRAM_RETRIES = _env_int("YTDLP_INSTAGRAM_RETRIES", 8)
    YTDLP_INSTAGRAM_FRAGMENT_RETRIES = _env_int("YTDLP_INSTAGRAM_FRAGMENT_RETRIES", 8)
    YTDLP_INSTAGRAM_SOCKET_TIMEOUT = _env_int("YTDLP_INSTAGRAM_SOCKET_TIMEOUT", 30)
    YTDLP_YOUTUBE_PLAYER_CLIENT = _env_str("YTDLP_YOUTUBE_PLAYER_CLIENT", "")
    # Keep this empty when it is not configured so the legacy single-client
    # setting can still take effect. The default chain is selected by the
    # downloader, where "default" has an explicit meaning.
    YTDLP_YOUTUBE_PLAYER_CLIENTS = _env_str("YTDLP_YOUTUBE_PLAYER_CLIENTS", "")


reload_from_environment()
