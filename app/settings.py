from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Load a small, shell-free subset of .env for non-Docker launches."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value[:1] in {'"', "'"}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


@dataclass(frozen=True)
class Settings:
    token: str
    logs_chat_id: int | None
    data_dir: Path
    output_dir: Path
    logs_dir: Path
    cookies_file: Path | None
    max_filesize: int
    workers: int
    max_queue: int
    upload_workers: int
    concurrent_fragments: int
    job_timeout: int
    disk_cache_max_files: int
    disk_cache_ttl: int
    file_id_cache_max_items: int
    file_id_cache_ttl_days: int
    media_cache_enabled: bool
    delete_original: bool
    default_language: str
    log_level: str


def load_settings(base_dir: Path | None = None) -> Settings:
    base = (base_dir or Path(__file__).resolve().parents[1]).resolve()
    _load_dotenv(base / ".env")

    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")

    logs_raw = (os.getenv("LOGS_CHAT_ID") or "").strip()
    try:
        logs_chat_id = int(logs_raw) if logs_raw else None
    except ValueError as exc:
        raise RuntimeError("LOGS_CHAT_ID must be an integer") from exc

    default_language = (os.getenv("DEFAULT_LANGUAGE") or "en").strip().lower()
    if default_language not in {"en", "ru"}:
        raise RuntimeError("DEFAULT_LANGUAGE must be en or ru")

    data_dir = Path(os.getenv("DATA_DIR") or base / "data").expanduser().resolve()
    output_dir = Path(os.getenv("OUTPUT_FOLDER") or data_dir / "cache").expanduser().resolve()
    logs_dir = Path(os.getenv("LOGS_DIR") or base / "logs").expanduser().resolve()
    cookies_raw = (os.getenv("COOKIES_FILE") or "").strip()
    log_level = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise RuntimeError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")

    return Settings(
        token=token,
        logs_chat_id=logs_chat_id,
        data_dir=data_dir,
        output_dir=output_dir,
        logs_dir=logs_dir,
        cookies_file=Path(cookies_raw).expanduser().resolve() if cookies_raw else None,
        max_filesize=_integer("MAX_FILESIZE", 50 * 1024 * 1024, 1024 * 1024, 2 * 1024**3),
        workers=_integer("WORKERS", 2, 1, 16),
        max_queue=_integer("MAX_QUEUE", 200, 1, 10_000),
        upload_workers=_integer("UPLOAD_WORKERS", 2, 1, 16),
        concurrent_fragments=_integer("YTDLP_CONCURRENT_FRAGMENTS", 4, 1, 16),
        job_timeout=_integer("JOB_TIMEOUT_SECONDS", 900, 30, 86_400),
        disk_cache_max_files=_integer("DISK_CACHE_MAX_FILES", 5, 1, 100),
        disk_cache_ttl=_integer("DISK_CACHE_TTL_SECONDS", 300, 30, 86_400),
        file_id_cache_max_items=_integer("FILE_ID_CACHE_MAX_ITEMS", 500, 1, 100_000),
        file_id_cache_ttl_days=_integer("FILE_ID_CACHE_TTL_DAYS", 30, 1, 3650),
        media_cache_enabled=_boolean("MEDIA_CACHE_ENABLED", True),
        delete_original=_boolean("DELETE_ORIGINAL", True),
        default_language=default_language,
        log_level=log_level,
    )
