from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.storage import JsonFile, Storage


def test_migrates_legacy_preferences(tmp_path) -> None:
    (tmp_path / "prefs.json").write_text(
        json.dumps(
            {
                "opt_out": {"-1": {"42": True}},
                "welcomed_groups": {"-1": True},
                "welcomed_private": {"42": True},
            }
        ),
        encoding="utf-8",
    )
    storage = Storage(tmp_path)
    assert storage.is_opted_out(-1, 42)
    assert storage.was_welcomed("group", -1)
    assert storage.was_welcomed("private", 42)
    assert (tmp_path / "prefs.json").exists()


def test_preferences_and_language_are_separate(tmp_path) -> None:
    storage = Storage(tmp_path)
    assert storage.chat_language(-10) == "en"
    storage.set_chat_language(-10, "ru")
    assert storage.chat_language(-10) == "ru"
    storage.set_delete_original(-10, False)
    assert storage.delete_original(-10) is False
    assert storage.toggle_opt_out(-10, 7) is True
    assert storage.toggle_opt_out(-10, 7) is False
    assert "language" in (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert "opt_out" in (tmp_path / "users.json").read_text(encoding="utf-8")


def test_known_groups_filter_private_ids_and_migrate_group_preferences(tmp_path) -> None:
    storage = Storage(tmp_path)
    storage.set_chat_language(-123, "ru")
    storage.set_delete_original(-123, False)
    storage.toggle_opt_out(-123, 7)
    storage.mark_welcomed("group", -123)
    storage.set_chat_language(42, "ru")

    assert storage.known_group_ids() == {-123}

    storage.migrate_chat_id(-123, -100123)

    assert storage.chat_language(-100123) == "ru"
    assert storage.delete_original(-100123) is False
    assert storage.is_opted_out(-100123, 7)
    assert storage.was_welcomed("group", -100123)
    assert -123 not in storage.known_group_ids()


def test_corrupt_store_recovers_from_backup(tmp_path) -> None:
    path = tmp_path / "value.json"
    store = JsonFile(path, lambda: {"version": 1, "value": 0})
    store.update(lambda data: data.__setitem__("value", 1))
    store.update(lambda data: data.__setitem__("value", 2))
    path.write_text("{broken", encoding="utf-8")
    recovered = JsonFile(path, lambda: {"version": 1, "value": 0})
    assert recovered.snapshot()["value"] == 1
    assert list(tmp_path.glob("value.json.corrupt-*"))


def test_file_id_cache_expiry_and_limit(tmp_path) -> None:
    storage = Storage(tmp_path)
    storage.put_file_id("a", "id-a", max_items=2)
    storage.put_file_id("b", "id-b", max_items=2)
    storage.put_file_id("c", "id-c", max_items=2)
    assert storage.get_file_id("a", 30) is None
    assert storage.get_file_id("c", 30) == "id-c"

    expired = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    storage.media.update(lambda data: data["items"]["c"].__setitem__("created_at", expired))
    assert storage.get_file_id("c", 1) is None


def test_media_alias_is_hashed(tmp_path) -> None:
    storage = Storage(tmp_path)
    secret_url = "https://example.com/video?token=secret"
    storage.put_file_id("media", "file-id", 10, source_name="Example", url_keys={secret_url})
    assert storage.get_cached_by_url(secret_url, 30) == ("media", "file-id", "Example")
    assert secret_url not in (tmp_path / "media_cache.json").read_text(encoding="utf-8")


def test_prunes_expired_media_and_aliases(tmp_path) -> None:
    storage = Storage(tmp_path)
    storage.put_file_id("old", "file-id", 10, url_keys={"url"})
    expired = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    storage.media.update(lambda data: data["items"]["old"].__setitem__("created_at", expired))
    storage.prune_media_cache(ttl_days=1, max_items=10)
    snapshot = storage.media.snapshot()
    assert snapshot["items"] == {}
    assert snapshot["aliases"] == {}
