from __future__ import annotations

import logging
import re
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


class RedactingFormatter(logging.Formatter):
    _url_query = re.compile(r"(https?://[^\s?]+)\?[^\s]+", re.IGNORECASE)

    def format(self, record: logging.LogRecord) -> str:
        return self._url_query.sub(r"\1?<redacted>", super().format(record))


def configure_logging(logs_dir: Path, level: str = "INFO") -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    root.handlers.clear()

    formatter = RedactingFormatter(
        "%(asctime)sZ %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)

    file_handler = TimedRotatingFileHandler(
        logs_dir / "bot.log",
        when="midnight",
        interval=1,
        backupCount=60,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setFormatter(formatter)
    root.addHandler(stream)
    root.addHandler(file_handler)
    return logging.getLogger("link_downloader_bot")
