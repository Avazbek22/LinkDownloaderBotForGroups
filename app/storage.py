from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import shutil
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)


class JsonFile:
    def __init__(self, path: Path, default_factory: Callable[[], dict[str, Any]]) -> None:
        self.path = path
        self.backup_path = path.with_suffix(path.suffix + ".bak")
        self.default_factory = default_factory
        self.lock = threading.RLock()
        self._data = self._load()

    def _read(self, path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON root must be an object")
        return value

    def _load(self) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            data = self.default_factory()
            self._write(data, make_backup=False)
            return data
        try:
            return self._read(self.path)
        except (OSError, ValueError, json.JSONDecodeError):
            LOG.exception("invalid JSON store path=%s", self.path)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            corrupt = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
            try:
                os.replace(self.path, corrupt)
            except OSError:
                LOG.exception("cannot quarantine corrupt store path=%s", self.path)
            if self.backup_path.exists():
                try:
                    data = self._read(self.backup_path)
                    self._write(data, make_backup=False)
                    return data
                except (OSError, ValueError, json.JSONDecodeError):
                    LOG.exception("invalid JSON backup path=%s", self.backup_path)
            data = self.default_factory()
            self._write(data, make_backup=False)
            return data

    def _write(self, data: dict[str, Any], *, make_backup: bool = True) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if make_backup and self.path.exists():
            shutil.copy2(self.path, self.backup_path)
        os.replace(tmp, self.path)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self._data)

    def update(self, mutator: Callable[[dict[str, Any]], Any]) -> Any:
        with self.lock:
            updated = copy.deepcopy(self._data)
            result = mutator(updated)
            if updated != self._data:
                self._write(updated)
                self._data = updated
            return result


class Storage:
    def __init__(self, data_dir: Path, default_language: str = "en", delete_original: bool = True) -> None:
        self.data_dir = data_dir
        self.default_language = default_language
        self.default_delete_original = delete_original
        self.settings = JsonFile(data_dir / "settings.json", lambda: {"version": 1, "chats": {}})
        self.users = JsonFile(data_dir / "users.json", lambda: {"version": 1, "opt_out": {}})
        self.state = JsonFile(
            data_dir / "state.json",
            lambda: {"version": 1, "welcomed_groups": {}, "welcomed_private": {}, "migrations": {}},
        )
        self.media = JsonFile(data_dir / "media_cache.json", lambda: {"version": 1, "items": {}, "aliases": {}})
        self._migrate_legacy()

    def _migrate_legacy(self) -> None:
        legacy = self.data_dir / "prefs.json"
        state = self.state.snapshot()
        if state.get("migrations", {}).get("prefs_v2") or not legacy.exists():
            return
        try:
            old = json.loads(legacy.read_text(encoding="utf-8"))
            if not isinstance(old, dict):
                raise ValueError("legacy JSON root must be an object")
            old_opt_out = old.get("opt_out", {}) if isinstance(old.get("opt_out"), dict) else {}
            old_groups = old.get("welcomed_groups", {}) if isinstance(old.get("welcomed_groups"), dict) else {}
            old_private = old.get("welcomed_private", {}) if isinstance(old.get("welcomed_private"), dict) else {}

            def migrate_users(data: dict[str, Any]) -> None:
                current = data.setdefault("opt_out", {})
                for chat_id, legacy_users in old_opt_out.items():
                    if isinstance(legacy_users, dict):
                        current.setdefault(str(chat_id), {}).update(copy.deepcopy(legacy_users))

            self.users.update(migrate_users)

            def migrate_state(data: dict[str, Any]) -> None:
                data.setdefault("welcomed_groups", {}).update(copy.deepcopy(old_groups))
                data.setdefault("welcomed_private", {}).update(copy.deepcopy(old_private))
                data.setdefault("migrations", {})["prefs_v2"] = datetime.now(UTC).isoformat()

            self.state.update(migrate_state)
            LOG.info("migrated legacy preferences path=%s", legacy)
        except (OSError, ValueError, json.JSONDecodeError):
            LOG.exception("cannot migrate legacy preferences path=%s", legacy)

    def chat_language(self, chat_id: int) -> str:
        chat = self.settings.snapshot().get("chats", {}).get(str(chat_id), {})
        language = chat.get("language") if isinstance(chat, dict) else None
        return language if language in {"en", "ru"} else self.default_language

    def set_chat_language(self, chat_id: int, language: str) -> None:
        if language not in {"en", "ru"}:
            raise ValueError("unsupported language")

        def mutate(data: dict[str, Any]) -> None:
            data.setdefault("chats", {}).setdefault(str(chat_id), {})["language"] = language

        self.settings.update(mutate)

    def delete_original(self, chat_id: int) -> bool:
        chat = self.settings.snapshot().get("chats", {}).get(str(chat_id), {})
        if isinstance(chat, dict) and isinstance(chat.get("delete_original"), bool):
            return chat["delete_original"]
        return self.default_delete_original

    def set_delete_original(self, chat_id: int, enabled: bool) -> None:
        def mutate(data: dict[str, Any]) -> None:
            data.setdefault("chats", {}).setdefault(str(chat_id), {})["delete_original"] = enabled

        self.settings.update(mutate)

    def is_opted_out(self, chat_id: int, user_id: int) -> bool:
        users = self.users.snapshot().get("opt_out", {}).get(str(chat_id), {})
        return bool(users.get(str(user_id), False)) if isinstance(users, dict) else False

    def toggle_opt_out(self, chat_id: int, user_id: int) -> bool:
        result = False

        def mutate(data: dict[str, Any]) -> None:
            nonlocal result
            root = data.setdefault("opt_out", {})
            users = root.setdefault(str(chat_id), {})
            result = not bool(users.get(str(user_id), False))
            if result:
                users[str(user_id)] = True
            else:
                users.pop(str(user_id), None)
                if not users:
                    root.pop(str(chat_id), None)

        self.users.update(mutate)
        return result

    def was_welcomed(self, kind: str, identity: int) -> bool:
        key = "welcomed_groups" if kind == "group" else "welcomed_private"
        return bool(self.state.snapshot().get(key, {}).get(str(identity), False))

    def mark_welcomed(self, kind: str, identity: int) -> None:
        key = "welcomed_groups" if kind == "group" else "welcomed_private"
        self.state.update(lambda data: data.setdefault(key, {}).__setitem__(str(identity), True))

    def known_group_ids(self) -> set[int]:
        """Return historical group IDs without treating them as current membership."""
        settings_chats = self.settings.snapshot().get("chats", {})
        welcomed_groups = self.state.snapshot().get("welcomed_groups", {})
        candidates = set(settings_chats) if isinstance(settings_chats, dict) else set()
        if isinstance(welcomed_groups, dict):
            candidates.update(welcomed_groups)
        result: set[int] = set()
        for value in candidates:
            try:
                chat_id = int(value)
            except (TypeError, ValueError):
                continue
            if chat_id < 0:
                result.add(chat_id)
        return result

    def migrate_chat_id(self, old_chat_id: int, new_chat_id: int) -> None:
        """Move preferences when Telegram upgrades a basic group to a supergroup."""
        old_key = str(int(old_chat_id))
        new_key = str(int(new_chat_id))
        if old_key == new_key:
            return

        def migrate_settings(data: dict[str, Any]) -> None:
            chats = data.setdefault("chats", {})
            old = chats.pop(old_key, None)
            if not isinstance(old, dict):
                return
            current = chats.get(new_key)
            merged = copy.deepcopy(old)
            if isinstance(current, dict):
                merged.update(current)
            chats[new_key] = merged

        def migrate_users(data: dict[str, Any]) -> None:
            groups = data.setdefault("opt_out", {})
            old = groups.pop(old_key, None)
            if not isinstance(old, dict):
                return
            current = groups.get(new_key)
            merged = copy.deepcopy(old)
            if isinstance(current, dict):
                merged.update(current)
            groups[new_key] = merged

        def migrate_state(data: dict[str, Any]) -> None:
            groups = data.setdefault("welcomed_groups", {})
            old = groups.pop(old_key, None)
            if old or new_key in groups:
                groups[new_key] = bool(old or groups.get(new_key))

        self.settings.update(migrate_settings)
        self.users.update(migrate_users)
        self.state.update(migrate_state)

    def get_file_id(self, media_key: str, ttl_days: int) -> str | None:
        item = self.media.snapshot().get("items", {}).get(media_key)
        if not isinstance(item, dict) or not isinstance(item.get("file_id"), str):
            return None
        try:
            created = datetime.fromisoformat(str(item["created_at"]))
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if datetime.now(UTC) - created > timedelta(days=ttl_days):
                self.remove_file_id(media_key)
                return None
        except (KeyError, ValueError, TypeError):
            self.remove_file_id(media_key)
            return None

        def touch(data: dict[str, Any]) -> None:
            current = data.setdefault("items", {}).get(media_key)
            if isinstance(current, dict):
                current["last_used_at"] = datetime.now(UTC).isoformat()

        self.media.update(touch)
        return item["file_id"]

    @staticmethod
    def _alias_key(url_key: str) -> str:
        return hashlib.sha256(url_key.encode("utf-8")).hexdigest()

    def get_cached_by_url(self, url_key: str, ttl_days: int) -> tuple[str, str, str] | None:
        snapshot = self.media.snapshot()
        media_key = snapshot.get("aliases", {}).get(self._alias_key(url_key))
        if not isinstance(media_key, str):
            return None
        file_id = self.get_file_id(media_key, ttl_days)
        if file_id is None:
            return None
        item = self.media.snapshot().get("items", {}).get(media_key, {})
        source_name = str(item.get("source_name") or "Video") if isinstance(item, dict) else "Video"
        return media_key, file_id, source_name

    def put_file_id(
        self,
        media_key: str,
        file_id: str,
        max_items: int,
        *,
        source_name: str = "Video",
        url_keys: set[str] | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()

        def mutate(data: dict[str, Any]) -> None:
            items = data.setdefault("items", {})
            aliases = data.setdefault("aliases", {})
            items[media_key] = {
                "file_id": file_id,
                "source_name": source_name,
                "created_at": now,
                "last_used_at": now,
            }
            for url_key in url_keys or set():
                aliases[self._alias_key(url_key)] = media_key
            while len(items) > max_items:
                oldest = min(items, key=lambda key: str(items[key].get("last_used_at", "")))
                items.pop(oldest, None)
                aliases = {alias: target for alias, target in aliases.items() if target != oldest}
                data["aliases"] = aliases

        self.media.update(mutate)

    def remove_file_id(self, media_key: str) -> None:
        def mutate(data: dict[str, Any]) -> None:
            data.setdefault("items", {}).pop(media_key, None)
            data["aliases"] = {
                alias: target for alias, target in data.setdefault("aliases", {}).items() if target != media_key
            }

        self.media.update(mutate)

    def prune_media_cache(self, ttl_days: int, max_items: int) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=ttl_days)

        def mutate(data: dict[str, Any]) -> None:
            items = data.setdefault("items", {})
            for media_key, item in list(items.items()):
                try:
                    created = datetime.fromisoformat(str(item["created_at"]))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=UTC)
                    if created < cutoff:
                        items.pop(media_key, None)
                except (KeyError, TypeError, ValueError):
                    items.pop(media_key, None)
            while len(items) > max_items:
                oldest = min(items, key=lambda key: str(items[key].get("last_used_at", "")))
                items.pop(oldest, None)
            data["aliases"] = {
                alias: target for alias, target in data.setdefault("aliases", {}).items() if target in items
            }

        self.media.update(mutate)
