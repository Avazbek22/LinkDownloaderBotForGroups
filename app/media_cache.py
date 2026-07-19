from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from pathlib import Path

LOG = logging.getLogger(__name__)


class DiskMediaCache:
    def __init__(self, directory: Path, max_files: int = 5, ttl_seconds: int = 300) -> None:
        self.directory = directory
        self.max_files = max_files
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def prefix(media_key: str) -> str:
        return "media-" + hashlib.sha256(media_key.encode("utf-8")).hexdigest()[:24]

    def get(self, media_key: str) -> Path | None:
        with self._lock:
            now = time.time()
            candidates = [path for path in self.directory.glob(f"{self.prefix(media_key)}.*") if self._usable(path)]
            if not candidates:
                return None
            path = max(candidates, key=lambda item: item.stat().st_mtime)
            if now - path.stat().st_mtime > self.ttl_seconds:
                self._unlink(path)
                return None
            os.utime(path, (now, now))
            return path

    def maintain(self) -> None:
        with self._lock:
            now = time.time()
            files = [path for path in self.directory.iterdir() if self._usable(path)]
            for path in files:
                try:
                    if now - path.stat().st_mtime > self.ttl_seconds:
                        self._unlink(path)
                except OSError:
                    LOG.exception("cannot inspect cache file path=%s", path)
            files = [path for path in self.directory.iterdir() if self._usable(path)]
            files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
            for path in files[self.max_files :]:
                self._unlink(path)
            self._remove_stale_intermediates(now)

    def remove_prefix_except(self, prefix: str, keep: Path | None = None) -> None:
        with self._lock:
            for path in self.directory.glob(f"{prefix}.*"):
                if keep is not None and path == keep:
                    continue
                self._unlink(path)

    def _remove_stale_intermediates(self, now: float) -> None:
        for path in self.directory.iterdir():
            if not path.is_file() or self._usable(path):
                continue
            try:
                if now - path.stat().st_mtime > self.ttl_seconds:
                    self._unlink(path)
            except OSError:
                LOG.exception("cannot inspect temporary file path=%s", path)

    @staticmethod
    def _usable(path: Path) -> bool:
        lower = path.name.lower()
        return path.is_file() and not lower.endswith((".part", ".ytdl", ".tmp", ".temp", ".meta")) and ".f" not in lower

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            LOG.exception("cannot remove cache file path=%s", path)
