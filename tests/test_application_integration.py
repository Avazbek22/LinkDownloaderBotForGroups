from __future__ import annotations

import queue
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import main
from app.download_backend import (
    DownloadCancelled,
    InstagramContentRestrictedError,
    MediaMetadata,
    RequestedMediaNotFoundError,
    SourceRateLimitedError,
)
from app.jobs import Job
from app.settings import Settings


class FakeBot:
    def __init__(self) -> None:
        self.sends = []
        self.audio_sends = []
        self.deletes = []
        self.reactions = []
        self.messages = []

    def send_video(self, *, video, **kwargs):
        self.sends.append((video, kwargs))
        return SimpleNamespace(video=SimpleNamespace(file_id="telegram-file-id"))

    def send_audio(self, *, audio, **kwargs):
        self.audio_sends.append((audio, kwargs))
        return SimpleNamespace(audio=SimpleNamespace(file_id="telegram-audio-file-id"))

    def delete_message(self, chat_id, message_id):
        self.deletes.append((chat_id, message_id))

    def set_message_reaction(self, chat_id, message_id, reaction, **kwargs):
        emoji = reaction[0].emoji if reaction else None
        self.reactions.append((chat_id, message_id, emoji, kwargs))
        return True

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))
        return True


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        token="123456:abcdefghijklmnopqrstuvwxyz",
        logs_chat_id=None,
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "cache",
        logs_dir=tmp_path / "logs",
        cookies_file=None,
        max_filesize=50_000_000,
        workers=2,
        max_queue=200,
        upload_workers=2,
        concurrent_fragments=4,
        job_timeout=60,
        disk_cache_max_files=5,
        disk_cache_ttl=300,
        file_id_cache_max_items=500,
        file_id_cache_ttl_days=30,
        media_cache_enabled=True,
        status_reactions=True,
        delete_original=True,
        default_language="en",
        log_level="INFO",
    )


def _reaction_update(
    emoji: str,
    *,
    chat_id: int = -100,
    message_id: int = 42,
    user_id: int = 99,
    old: tuple[str, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        message_id=message_id,
        user=SimpleNamespace(id=user_id, is_bot=False),
        old_reaction=[SimpleNamespace(type="emoji", emoji=item) for item in old],
        new_reaction=[SimpleNamespace(type="emoji", emoji=item) for item in (*old, emoji)],
    )


def test_three_requests_download_and_upload_once(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    flight = None
    for index in range(3):
        job = Job(
            str(index),
            -(index + 1),
            None,
            index + 10,
            index + 20,
            "https://example.com/video",
            "https://example.com/video",
            f"User {index}",
            True,
        )
        submitted = app.coordinator.submit(job)
        flight = submitted or flight
    assert flight is not None

    metadata = MediaMetadata(
        url="https://example.com/video",
        info={
            "id": "video",
            "extractor": "Test",
            "formats": [{"format_id": "video", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a"}],
        },
        media_key="test:video",
        source_name="Test",
    )
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    calls = {"downloads": 0}

    monkeypatch.setattr(main, "validate_public_url", lambda url: url)
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: metadata)

    def obtain(*_args):
        calls["downloads"] += 1
        return video_path

    monkeypatch.setattr(app, "_obtain_file", obtain)
    app._process_flight(flight)

    assert calls["downloads"] == 1
    assert len(fake.sends) == 3
    assert not isinstance(fake.sends[0][0], str)
    assert fake.sends[1][0] == "telegram-file-id"
    assert fake.sends[2][0] == "telegram-file-id"
    assert len(fake.deletes) == 3

    delayed = Job(
        "delayed",
        -4,
        None,
        20,
        30,
        "https://example.com/video",
        "https://example.com/video",
        "Later User",
        True,
    )
    delayed_flight = app.coordinator.submit(delayed)
    assert delayed_flight is not None
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: (_ for _ in ()).throw(AssertionError("metadata call")))
    app._process_flight(delayed_flight)
    assert calls["downloads"] == 1
    assert fake.sends[3][0] == "telegram-file-id"


def test_extract_first_url_trims_punctuation() -> None:
    assert main.extract_first_url("look (https://example.com/video).") == "https://example.com/video"


def test_pausing_group_invalidates_queued_job_before_metadata_probe(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    app.group_registry.record_bootstrap_result(
        -100,
        title="Paused",
        chat_type="supergroup",
        telegram_status="member",
    )
    job = Job(
        "queued",
        -100,
        None,
        42,
        7,
        "https://example.com/video",
        "https://example.com/video",
        "User",
        True,
        runtime_revision=0,
    )
    app._set_status_reaction(job, "👀")
    flight = app.coordinator.submit(job)
    assert flight is not None
    result, _group = app.group_registry.set_runtime_mode(-100, "soft", 42, expected_revision=0)
    assert result == "changed"
    app._invalidate_group_work(-100)
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: (_ for _ in ()).throw(AssertionError("probed")))

    app._process_flight(flight)

    assert fake.sends == []
    assert fake.deletes == []
    assert fake.reactions[0][2] == "👀"
    assert fake.reactions[-1][2] is None


def test_pausing_one_group_does_not_cancel_shared_delivery_to_another(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    for chat_id in (-100, -200):
        app.group_registry.record_bootstrap_result(
            chat_id,
            title=f"Group {chat_id}",
            chat_type="supergroup",
            telegram_status="member",
        )
    paused = Job(
        "paused",
        -100,
        None,
        42,
        7,
        "https://example.com/video",
        "https://example.com/video",
        "Paused User",
        True,
        runtime_revision=0,
    )
    active = Job(
        "active",
        -200,
        None,
        43,
        8,
        "https://example.com/video",
        "https://example.com/video",
        "Active User",
        True,
        runtime_revision=0,
    )
    flight = app.coordinator.submit(paused)
    assert flight is not None
    assert app.coordinator.submit(active) is None
    app._set_status_reaction(paused, "👀")
    app._set_status_reaction(active, "👀")
    app.group_registry.set_runtime_mode(-100, "soft", 42, expected_revision=0)
    app._invalidate_group_work(-100)
    metadata = MediaMetadata(
        url=active.url,
        info={
            "id": "video",
            "extractor": "Test",
            "formats": [{"format_id": "video", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a"}],
        },
        media_key="test:video",
        source_name="Test",
    )
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    monkeypatch.setattr(main, "validate_public_url", lambda url: url)
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: metadata)
    monkeypatch.setattr(app, "_obtain_file", lambda *_args: video_path)

    app._process_flight(flight)

    assert [kwargs["chat_id"] for _video, kwargs in fake.sends] == [-200]
    assert fake.deletes == [(-200, 43)]


def test_pausing_group_clears_and_disables_existing_failure_retry(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    app.group_registry.record_bootstrap_result(
        -100,
        title="Paused",
        chat_type="supergroup",
        telegram_status="member",
    )
    job = Job(
        "failed",
        -100,
        None,
        42,
        7,
        "https://example.com/video",
        "https://example.com/video",
        "User",
        True,
        runtime_revision=0,
    )
    app._after_failure(job)

    app.group_registry.set_runtime_mode(-100, "soft", 42, expected_revision=0)
    app._invalidate_group_work(-100)
    app._handle_retry_reaction(_reaction_update("👎"))

    assert [item[2] for item in fake.reactions] == ["👎", None]
    assert app.queue.empty()


def test_group_command_recognizes_only_commands_for_this_bot() -> None:
    assert main.BotApplication._group_command(" /SKIP https://example.com", "downloader") == "skip"
    assert main.BotApplication._group_command("/skip@Downloader https://example.com", "downloader") == "skip"
    assert main.BotApplication._group_command("/skip@another_bot https://example.com", "downloader") == ""
    assert main.BotApplication._group_command("https://example.com /skip", "downloader") is None


def test_skip_command_leaves_message_completely_untouched(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    app.bot_username = "downloader"
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100, type="supergroup"),
        from_user=SimpleNamespace(id=7, is_bot=False, first_name="User", last_name="", username="user"),
        text="/skip@Downloader https://example.com/video",
        caption=None,
        message_id=42,
    )
    monkeypatch.setattr(
        main,
        "validate_public_url",
        lambda _url: (_ for _ in ()).throw(AssertionError("skip must not validate the URL")),
    )

    app._handle_group_message(message)

    assert app.queue.empty()
    assert fake.reactions == []
    assert fake.sends == []
    assert fake.deletes == []
    assert fake.messages == []
    assert not app.storage.is_opted_out(-100, 7)


def test_command_for_another_bot_never_downloads_its_link(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    app.bot_username = "downloader"
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100, type="supergroup"),
        from_user=SimpleNamespace(id=7, is_bot=False, first_name="User", last_name="", username="user"),
        text="/skip@another_bot https://example.com/video",
        caption=None,
        message_id=42,
    )
    monkeypatch.setattr(
        main,
        "validate_public_url",
        lambda _url: (_ for _ in ()).throw(AssertionError("foreign commands must be ignored")),
    )

    app._handle_group_message(message)

    assert app.queue.empty()
    assert fake.reactions == []
    assert fake.sends == []
    assert fake.deletes == []


def test_audio_command_queues_one_audio_request_even_for_opted_out_user(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    app.bot_username = "downloader"
    app.storage.set_opt_out(-100, 7, True)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100, type="supergroup"),
        from_user=SimpleNamespace(id=7, is_bot=False, first_name="User", last_name="", username="user"),
        text="/audio@Downloader https://example.com/video",
        caption=None,
        message_id=42,
    )
    monkeypatch.setattr(main, "validate_public_url", lambda url: url)

    app._handle_group_message(message)

    flight = app.queue.get_nowait()
    assert flight is not None
    assert flight.media_kind == "audio"
    assert len(flight.jobs) == 1
    assert flight.jobs[0].media_kind == "audio"
    assert app.storage.is_opted_out(-100, 7)
    assert [item[2] for item in fake.reactions] == ["👀"]
    app.coordinator.abort(flight)


def test_audio_command_without_link_shows_usage_without_reaction(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100, type="supergroup"),
        from_user=SimpleNamespace(id=7, is_bot=False, first_name="User", last_name="", username="user"),
        text="/audio",
        caption=None,
        message_id=42,
    )

    app._handle_group_message(message)

    assert app.queue.empty()
    assert fake.reactions == []
    assert len(fake.messages) == 1
    assert "/audio <link>" in fake.messages[0][1]


def test_audio_request_uploads_and_reuses_only_audio_cache(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    url = "https://example.com/video"
    metadata = MediaMetadata(
        url=url,
        info={
            "id": "media",
            "duration": 60,
            "title": "Track title",
            "artist": "Track artist",
            "formats": [{"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a"}],
        },
        media_key="example:media",
        source_name="Example",
    )
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"audio")
    first = Job(
        "audio-1",
        -100,
        17,
        42,
        7,
        url,
        url,
        "Original User",
        True,
        media_kind="audio",
    )
    flight = app.coordinator.submit(first)
    assert flight is not None
    monkeypatch.setattr(main, "validate_public_url", lambda value: value)
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: metadata)
    monkeypatch.setattr(app, "_obtain_file", lambda *_args: audio_path)

    app._process_flight(flight)

    audio_profile = f"{url}|mp3-v1:{app.settings.max_filesize}"
    video_profile = f"{url}|mp4-h264-v2:{app.settings.max_filesize}"
    assert app.storage.get_cached_by_url(audio_profile, app.settings.file_id_cache_ttl_days) is not None
    assert app.storage.get_cached_by_url(video_profile, app.settings.file_id_cache_ttl_days) is None
    assert len(fake.audio_sends) == 1
    assert fake.sends == []
    assert fake.deletes == [(-100, 42)]
    assert fake.audio_sends[0][1]["message_thread_id"] == 17
    assert fake.audio_sends[0][1]["title"] == "Track title"
    assert fake.audio_sends[0][1]["performer"] == "Track artist"
    assert fake.audio_sends[0][1]["duration"] == 60
    assert "Original audio" in fake.audio_sends[0][1]["caption"]

    second = Job(
        "audio-2",
        -200,
        None,
        43,
        8,
        url,
        url,
        "Later User",
        True,
        media_kind="audio",
    )
    cached_flight = app.coordinator.submit(second)
    assert cached_flight is not None
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: (_ for _ in ()).throw(AssertionError("probe")))

    app._process_flight(cached_flight)

    assert len(fake.audio_sends) == 2
    assert fake.audio_sends[1][0] == "telegram-audio-file-id"
    assert fake.deletes == [(-100, 42), (-200, 43)]


def test_youtube_cache_profiles_do_not_reuse_preference_agnostic_audio(tmp_path) -> None:
    max_filesize = _settings(tmp_path).max_filesize

    assert main._media_cache_profile("https://youtu.be/video", "video", max_filesize) == (
        f"mp4-h264-v2-youtube-original-audio-v1:{max_filesize}"
    )
    assert main._media_cache_profile("https://www.youtube.com/watch?v=audio", "audio", max_filesize) == (
        f"mp3-v1-youtube-original-audio-v1:{max_filesize}"
    )
    assert main._media_cache_profile("https://example.com/video", "video", max_filesize) == (
        f"mp4-h264-v2:{max_filesize}"
    )


def test_youtube_request_bypasses_old_translated_video_cache(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    url = "https://www.youtube.com/watch?v=original"
    old_profile = f"mp4-h264-v2:{app.settings.max_filesize}"
    app.storage.put_file_id(
        "youtube:original:old-profile",
        "translated-file-id",
        app.settings.file_id_cache_max_items,
        source_name="YouTube",
        url_keys={f"{url}|{old_profile}"},
    )
    metadata = MediaMetadata(
        url=url,
        info={
            "id": "original",
            "formats": [{"format_id": "18", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a"}],
        },
        media_key="youtube:original",
        source_name="YouTube",
    )
    video_path = tmp_path / "original.mp4"
    video_path.write_bytes(b"video")
    job = Job("original", -100, None, 42, 7, url, url, "User", True)
    flight = app.coordinator.submit(job)
    assert flight is not None
    monkeypatch.setattr(main, "validate_public_url", lambda value: value)
    monkeypatch.setattr(main, "extract_metadata", lambda *_args, **_kwargs: metadata)
    monkeypatch.setattr(app, "_obtain_file", lambda *_args: video_path)

    app._process_flight(flight)

    assert len(fake.sends) == 1
    assert fake.sends[0][0] != "translated-file-id"
    new_profile = main._media_cache_profile(url, "video", app.settings.max_filesize)
    assert app.storage.get_cached_by_url(f"{url}|{new_profile}", app.settings.file_id_cache_ttl_days) is not None


def test_status_reaction_lifecycle_for_retained_link(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    job = Job(
        "reaction",
        -100,
        None,
        42,
        7,
        "https://example.com/video",
        "https://example.com/video",
        "User",
        False,
    )

    app._set_status_reaction(job, "👀")
    app._after_success(job)
    app._after_failure(job)

    assert [item[2] for item in fake.reactions] == ["👀", "👍", "👎"]
    assert fake.deletes == []


def test_successful_deleted_link_needs_no_success_reaction(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    job = Job(
        "deleted",
        -100,
        None,
        42,
        7,
        "https://example.com/video",
        "https://example.com/video",
        "User",
        True,
    )

    app._set_status_reaction(job, "👀")
    app._after_success(job)

    assert [item[2] for item in fake.reactions] == ["👀"]
    assert fake.deletes == [(-100, 42)]


def test_reaction_failure_never_breaks_processing(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    job = Job("job", -100, None, 42, 7, "https://example.com", "key", "User", True)

    class ReactionsDisabledBot(FakeBot):
        def set_message_reaction(self, *_args, **_kwargs):
            raise RuntimeError("reactions disabled")

    app.bot = ReactionsDisabledBot()
    app._set_status_reaction(job, "👀")


def test_disallowed_failure_reaction_clears_stale_eyes(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    job = Job("job", -100, None, 42, 7, "https://example.com", "key", "User", True)

    class RestrictedBot(FakeBot):
        def set_message_reaction(self, chat_id, message_id, reaction, **kwargs):
            if reaction and reaction[0].emoji == "👎":
                raise RuntimeError("reaction not allowed")
            return super().set_message_reaction(chat_id, message_id, reaction, **kwargs)

    fake = RestrictedBot()
    app.bot = fake
    app._set_status_reaction(job, "👀")
    app._after_failure(job)
    app._handle_retry_reaction(_reaction_update("👎"))

    assert [item[2] for item in fake.reactions] == ["👀", None]
    assert app.queue.empty()


def test_reactions_can_be_disabled(tmp_path) -> None:
    app = main.BotApplication(replace(_settings(tmp_path), status_reactions=False))
    fake = FakeBot()
    app.bot = fake
    job = Job("job", -100, None, 42, 7, "https://example.com", "key", "User", True)

    app._set_status_reaction(job, "👀")

    assert fake.reactions == []


def test_matching_failure_reaction_queues_retry_and_replaces_bot_status(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    job = Job("failed", -100, 17, 42, 7, "https://example.com/video", "key", "Original User", True)
    app._after_failure(job)

    app._handle_retry_reaction(_reaction_update("👎"))

    flight = app.queue.get_nowait()
    assert flight is not None
    retry_job = flight.jobs[0]
    assert retry_job.job_id != job.job_id
    assert retry_job.chat_id == job.chat_id
    assert retry_job.message_thread_id == job.message_thread_id
    assert retry_job.original_message_id == job.original_message_id
    assert retry_job.user_id == job.user_id
    assert retry_job.url == job.url
    assert retry_job.sender_name == job.sender_name
    assert [item[2] for item in fake.reactions] == ["👎", "👀"]
    app.coordinator.abort(flight)


def test_audio_failure_retry_preserves_audio_request(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    job = Job(
        "failed-audio",
        -100,
        None,
        42,
        7,
        "https://example.com/video",
        "key",
        "User",
        True,
        media_kind="audio",
    )
    app._after_failure(job)

    app._handle_retry_reaction(_reaction_update("👎"))

    flight = app.queue.get_nowait()
    assert flight is not None
    assert flight.media_kind == "audio"
    assert flight.jobs[0].media_kind == "audio"
    app.coordinator.abort(flight)


def test_retry_requires_new_matching_reaction_from_human(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    job = Job("restricted", -100, None, 42, 7, "https://example.com/video", "key", "User", True)
    app._after_instagram_restriction(job)

    app._handle_retry_reaction(_reaction_update("👎"))
    app._handle_retry_reaction(_reaction_update("🙈", old=("🙈",)))
    bot_update = _reaction_update("🙈")
    bot_update.user.is_bot = True
    app._handle_retry_reaction(bot_update)

    assert app.queue.empty()
    assert [item[2] for item in fake.reactions] == ["🙈"]


def test_matching_monkey_reaction_queues_restricted_link_retry(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    job = Job("restricted", -100, None, 42, 7, "https://example.com/video", "key", "User", True)
    app._after_instagram_restriction(job)

    app._handle_retry_reaction(_reaction_update("🙈"))

    flight = app.queue.get_nowait()
    assert flight is not None
    assert [item[2] for item in fake.reactions] == ["🙈", "👀"]
    app.coordinator.abort(flight)


def test_expired_failure_reaction_does_not_retry(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    job = Job("failed", -100, None, 42, 7, "https://example.com/video", "key", "User", True)
    monkeypatch.setattr(main.time, "monotonic", lambda: 10.0)
    app._after_failure(job)
    monkeypatch.setattr(main.time, "monotonic", lambda: 10.0 + main.FAILED_RETRY_TTL_SECONDS + 1)

    app._handle_retry_reaction(_reaction_update("👎"))

    assert app.queue.empty()
    assert [item[2] for item in fake.reactions] == ["👎"]


def test_only_one_reaction_retry_can_be_active_for_a_message(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    job = Job("failed", -100, None, 42, 7, "https://example.com/video", "key", "User", True)
    app._after_failure(job)

    app._handle_retry_reaction(_reaction_update("👎", user_id=10))
    app._handle_retry_reaction(_reaction_update("👎", user_id=11))

    flight = app.queue.get_nowait()
    assert flight is not None
    assert app.queue.empty()
    assert [item[2] for item in fake.reactions] == ["👎", "👀"]
    app.coordinator.abort(flight)


def test_retry_probe_failure_restores_downvote_instead_of_clearing_status(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    job = Job("failed", -100, None, 42, 7, "https://example.com/video", "key", "User", True)
    app._after_failure(job)
    app._handle_retry_reaction(_reaction_update("👎"))
    flight = app.queue.get_nowait()
    assert flight is not None
    monkeypatch.setattr(main, "validate_public_url", lambda url: url)
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: (_ for _ in ()).throw(RuntimeError("failed")))

    app._process_flight(flight)

    assert [item[2] for item in fake.reactions] == ["👎", "👀", "👎"]
    app._handle_retry_reaction(_reaction_update("👎", user_id=101))
    second_flight = app.queue.get_nowait()
    assert second_flight is not None
    app.coordinator.abort(second_flight)


def test_full_queue_restores_failure_status_and_unlocks_retry(tmp_path) -> None:
    app = main.BotApplication(replace(_settings(tmp_path), max_queue=1))
    fake = FakeBot()
    app.bot = fake
    app.queue.put_nowait(None)
    job = Job("failed", -100, None, 42, 7, "https://example.com/video", "key", "User", True)
    app._after_failure(job)

    app._handle_retry_reaction(_reaction_update("👎", user_id=10))
    app._handle_retry_reaction(_reaction_update("👎", user_id=11))

    assert [item[2] for item in fake.reactions] == ["👎", "👀", "👎", "👀", "👎"]


def test_success_forgets_failed_retry_entry(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    job = Job("failed", -100, None, 42, 7, "https://example.com/video", "key", "User", False)
    app._after_failure(job)
    app._handle_retry_reaction(_reaction_update("👎"))
    flight = app.queue.get_nowait()
    assert flight is not None

    app._after_success(flight.jobs[0])
    app.coordinator.abort(flight)
    app._handle_retry_reaction(_reaction_update("👎", user_id=101))

    assert app.queue.empty()
    assert [item[2] for item in fake.reactions] == ["👎", "👀", "👍"]


def test_reaction_handler_is_registered(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))

    assert len(app.bot.message_reaction_handlers) == 1


def test_polling_explicitly_requests_reaction_updates(tmp_path, monkeypatch) -> None:
    class PollingBot(FakeBot):
        def get_updates(self, *args, **kwargs) -> list:
            return []

        def infinity_polling(self, **kwargs) -> None:
            self.polling_options = kwargs

    class DormantThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            pass

    app = main.BotApplication(_settings(tmp_path))
    fake = PollingBot()
    app.bot = fake
    app.health_marker = tmp_path / "health"
    monkeypatch.setattr(app, "initialize_identity", lambda: None)
    monkeypatch.setattr(app, "_set_commands", lambda: None)
    monkeypatch.setattr(main.threading, "Thread", DormantThread)

    app.start()

    assert fake.polling_options["allowed_updates"] == [
        "message",
        "message_reaction",
        "my_chat_member",
        "callback_query",
    ]


def test_accepted_group_link_gets_eyes(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100, type="supergroup"),
        from_user=SimpleNamespace(id=7, is_bot=False, first_name="User", last_name="", username="user"),
        text="https://example.com/video",
        caption=None,
        message_id=42,
    )
    monkeypatch.setattr(main, "validate_public_url", lambda url: url)

    app._handle_group_message(message)

    assert [item[2] for item in fake.reactions] == ["👀"]
    flight = app.queue.get_nowait()
    assert flight is not None
    app.coordinator.abort(flight)


def test_full_queue_clears_unconfirmed_eyes(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(replace(_settings(tmp_path), max_queue=1))
    fake = FakeBot()
    app.bot = fake
    app.queue.put_nowait(None)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100, type="supergroup"),
        from_user=SimpleNamespace(id=7, is_bot=False, first_name="User", last_name="", username="user"),
        text="https://example.com/video",
        caption=None,
        message_id=42,
    )
    monkeypatch.setattr(main, "validate_public_url", lambda url: url)

    app._handle_group_message(message)

    assert [item[2] for item in fake.reactions] == ["👀", None]


def test_full_queue_clears_eyes_for_jobs_that_joined_before_abort(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(replace(_settings(tmp_path), max_queue=1))
    fake = FakeBot()
    app.bot = fake
    joined = Job(
        "joined",
        -100,
        None,
        43,
        8,
        "https://example.com/video",
        "https://example.com/video",
        "Second User",
        True,
    )

    class FullAfterJoin:
        def put_nowait(self, _flight) -> None:
            app._set_status_reaction(joined, "👀")
            assert app.coordinator.submit(joined) is None
            raise queue.Full

    app.queue = FullAfterJoin()
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100, type="supergroup"),
        from_user=SimpleNamespace(id=7, is_bot=False, first_name="User", last_name="", username="user"),
        text="https://example.com/video",
        caption=None,
        message_id=42,
    )
    monkeypatch.setattr(main, "validate_public_url", lambda url: url)

    app._handle_group_message(message)

    by_message: dict[int, list[str | None]] = {}
    for _, message_id, emoji, _ in fake.reactions:
        by_message.setdefault(message_id, []).append(emoji)
    assert by_message == {42: ["👀", None], 43: ["👀", None]}


def test_metadata_failure_clears_eyes_for_every_joined_job(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    flight = None
    for index in range(2):
        job = Job(
            f"failed-{index}",
            -(index + 1),
            None,
            index + 10,
            index + 20,
            "https://example.com/video",
            "https://example.com/video",
            f"User {index}",
            True,
        )
        app._set_status_reaction(job, "👀")
        flight = app.coordinator.submit(job) or flight
    assert flight is not None
    monkeypatch.setattr(main, "validate_public_url", lambda url: url)
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: (_ for _ in ()).throw(RuntimeError("failed")))

    app._process_flight(flight)

    by_message: dict[int, list[str]] = {}
    for _, message_id, emoji, _ in fake.reactions:
        by_message.setdefault(message_id, []).append(emoji)
    assert by_message == {10: ["👀", None], 11: ["👀", None]}


def test_instagram_content_restriction_replaces_eyes_with_monkey(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    job = Job(
        "restricted",
        -100,
        None,
        42,
        7,
        "https://www.instagram.com/reel/restricted/",
        "https://www.instagram.com/reel/restricted",
        "User",
        True,
    )
    app._set_status_reaction(job, "👀")
    flight = app.coordinator.submit(job)
    assert flight is not None
    monkeypatch.setattr(main, "validate_public_url", lambda url: url)
    monkeypatch.setattr(
        main,
        "extract_metadata",
        lambda *_args: (_ for _ in ()).throw(InstagramContentRestrictedError("restricted")),
    )

    app._process_flight(flight)

    assert [item[2] for item in fake.reactions] == ["👀", "🙈"]
    assert fake.sends == []
    assert fake.deletes == []


def test_explicit_instagram_no_video_replaces_eyes_with_no_media_reaction(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    job = Job(
        "image-only",
        -100,
        None,
        42,
        7,
        "https://www.instagram.com/p/image-only/",
        "https://www.instagram.com/p/image-only",
        "User",
        True,
    )
    app._set_status_reaction(job, "👀")
    flight = app.coordinator.submit(job)
    assert flight is not None
    monkeypatch.setattr(main, "validate_public_url", lambda url: url)
    monkeypatch.setattr(
        main,
        "extract_metadata",
        lambda *_args: (_ for _ in ()).throw(RequestedMediaNotFoundError("no video")),
    )

    app._process_flight(flight)

    assert [item[2] for item in fake.reactions] == ["👀", "🤷"]
    assert fake.sends == []
    assert fake.deletes == []


def test_source_limit_replaces_eyes_and_short_circuits_later_requests(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    calls = 0

    def limited_probe(*_args):
        nonlocal calls
        calls += 1
        raise SourceRateLimitedError("instagram")

    monkeypatch.setattr(main, "validate_public_url", lambda url: url)
    monkeypatch.setattr(main, "extract_metadata", limited_probe)

    for index in range(2):
        job = Job(
            f"limited-{index}",
            -100,
            None,
            42 + index,
            7,
            f"https://www.instagram.com/reel/limited-{index}/",
            f"https://www.instagram.com/reel/limited-{index}",
            "User",
            True,
        )
        app._set_status_reaction(job, "👀")
        flight = app.coordinator.submit(job)
        assert flight is not None
        app._process_flight(flight)

    assert calls == 1
    by_message: dict[int, list[str]] = {}
    for _, message_id, emoji, _ in fake.reactions:
        by_message.setdefault(message_id, []).append(emoji)
    assert by_message == {42: ["👀", "😴"], 43: ["👀", "😴"]}
    assert fake.sends == []


def test_source_limit_during_download_replaces_eyes_with_sleeping_status(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    job = Job(
        "download-limited",
        -100,
        None,
        42,
        7,
        "https://www.youtube.com/watch?v=limited",
        "https://www.youtube.com/watch?v=limited",
        "User",
        True,
    )
    metadata = MediaMetadata(
        url=job.url,
        info={
            "id": "limited",
            "extractor": "Youtube",
            "formats": [{"format_id": "18", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a"}],
        },
        media_key="youtube:limited",
        source_name="YouTube",
    )
    app._set_status_reaction(job, "👀")
    flight = app.coordinator.submit(job)
    assert flight is not None
    monkeypatch.setattr(main, "validate_public_url", lambda url: url)
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: metadata)
    monkeypatch.setattr(
        app,
        "_obtain_file",
        lambda *_args: (_ for _ in ()).throw(SourceRateLimitedError("youtube")),
    )

    app._process_flight(flight)

    assert [item[2] for item in fake.reactions] == ["👀", "😴"]
    assert fake.sends == []


def test_telegram_cache_bypasses_active_source_cooldown(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    job = Job(
        "cached-during-cooldown",
        -100,
        None,
        42,
        7,
        "https://www.instagram.com/reel/cached/",
        "https://www.instagram.com/reel/cached",
        "User",
        True,
    )
    cache_profile = f"mp4-h264-v2:{app.settings.max_filesize}"
    app.storage.put_file_id(
        "instagram:cached:profile",
        "cached-file-id",
        app.settings.file_id_cache_max_items,
        source_name="Instagram",
        url_keys={f"{job.url_key}|{cache_profile}"},
    )
    app.source_cooldowns.limited("instagram")
    app._set_status_reaction(job, "👀")
    flight = app.coordinator.submit(job)
    assert flight is not None
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: (_ for _ in ()).throw(AssertionError("probe")))

    app._process_flight(flight)

    assert fake.sends[0][0] == "cached-file-id"
    assert fake.deletes == [(-100, 42)]
    assert [item[2] for item in fake.reactions] == ["👀"]


def test_invalid_telegram_cache_falls_back_to_sleeping_status_during_cooldown(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))

    class InvalidCacheBot(FakeBot):
        def send_video(self, *, video, **kwargs):
            if video == "invalid-file-id":
                raise RuntimeError("Bad Request: wrong file identifier")
            return super().send_video(video=video, **kwargs)

    fake = InvalidCacheBot()
    app.bot = fake
    job = Job(
        "invalid-cache",
        -100,
        None,
        42,
        7,
        "https://www.instagram.com/reel/invalid-cache/",
        "https://www.instagram.com/reel/invalid-cache",
        "User",
        True,
    )
    cache_profile = f"mp4-h264-v2:{app.settings.max_filesize}"
    app.storage.put_file_id(
        "instagram:invalid-cache:profile",
        "invalid-file-id",
        app.settings.file_id_cache_max_items,
        source_name="Instagram",
        url_keys={f"{job.url_key}|{cache_profile}"},
    )
    app.source_cooldowns.limited("instagram")
    app._set_status_reaction(job, "👀")
    flight = app.coordinator.submit(job)
    assert flight is not None
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: (_ for _ in ()).throw(AssertionError("probe")))

    app._process_flight(flight)

    assert [item[2] for item in fake.reactions] == ["👀", "😴"]
    fresh = app.coordinator.submit(replace(job, job_id="fresh"))
    assert fresh is not None
    app.coordinator.abort(fresh)


def test_sleeping_reaction_falls_back_to_downvote_when_unavailable(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))

    class NoSleepingReactionBot(FakeBot):
        def set_message_reaction(self, chat_id, message_id, reaction, **kwargs):
            emoji = reaction[0].emoji if reaction else None
            if emoji == "😴":
                raise RuntimeError("reaction not allowed")
            return super().set_message_reaction(chat_id, message_id, reaction, **kwargs)

    fake = NoSleepingReactionBot()
    app.bot = fake
    job = Job(
        "fallback",
        -100,
        None,
        42,
        7,
        "https://www.instagram.com/reel/limited/",
        "https://www.instagram.com/reel/limited",
        "User",
        True,
    )

    app._after_source_rate_limit(job)
    app._handle_retry_reaction(_reaction_update("👎"))

    flight = app.queue.get_nowait()
    assert flight is not None
    assert [item[2] for item in fake.reactions] == ["👎", "👀"]
    app.coordinator.abort(flight)


def test_sleeping_reaction_retry_never_bypasses_active_cooldown(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    job = Job(
        "sleeping-retry",
        -100,
        None,
        42,
        7,
        "https://www.youtube.com/watch?v=limited",
        "https://www.youtube.com/watch?v=limited",
        "User",
        True,
    )
    app.source_cooldowns.limited("youtube")
    app._after_source_rate_limit(job)
    app._handle_retry_reaction(_reaction_update("😴"))
    flight = app.queue.get_nowait()
    assert flight is not None
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: (_ for _ in ()).throw(AssertionError("probe")))

    app._process_flight(flight)

    assert [item[2] for item in fake.reactions] == ["😴", "👀", "😴"]
    assert fake.sends == []


def test_non_video_metadata_replaces_eyes_with_non_video_reaction(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    job = Job(
        "news",
        -100,
        None,
        42,
        7,
        "https://example.com/news",
        "https://example.com/news",
        "User",
        True,
    )
    app._set_status_reaction(job, "👀")
    flight = app.coordinator.submit(job)
    assert flight is not None
    metadata = MediaMetadata(
        url=job.url,
        info={
            "id": "article",
            "extractor": "Generic",
            "formats": [{"format_id": "audio", "ext": "m4a", "vcodec": "none", "acodec": "aac"}],
        },
        media_key="generic:article",
        source_name="News",
    )
    monkeypatch.setattr(main, "validate_public_url", lambda url: url)
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: metadata)

    app._process_flight(flight)

    assert [item[2] for item in fake.reactions] == ["👀", "🤷"]
    assert fake.sends == []
    assert fake.deletes == []


def test_audio_request_without_audio_replaces_eyes_with_no_media_reaction(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    job = Job(
        "silent-video",
        -100,
        None,
        42,
        7,
        "https://example.com/video",
        "https://example.com/video",
        "User",
        True,
        media_kind="audio",
    )
    app._set_status_reaction(job, "👀")
    flight = app.coordinator.submit(job)
    assert flight is not None
    metadata = MediaMetadata(
        url=job.url,
        info={
            "id": "silent-video",
            "duration": 60,
            "formats": [{"format_id": "video", "ext": "mp4", "vcodec": "avc1", "acodec": "none"}],
        },
        media_key="generic:silent-video",
        source_name="Example",
    )
    monkeypatch.setattr(main, "validate_public_url", lambda url: url)
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: metadata)

    app._process_flight(flight)

    assert [item[2] for item in fake.reactions] == ["👀", "🤷"]
    assert fake.audio_sends == []
    assert fake.deletes == []


def test_unavailable_non_video_reaction_clears_stale_eyes(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))

    class RestrictedBot(FakeBot):
        def set_message_reaction(self, chat_id, message_id, reaction, **kwargs):
            if reaction and reaction[0].emoji == "🤷":
                raise RuntimeError("reaction not allowed")
            return super().set_message_reaction(chat_id, message_id, reaction, **kwargs)

    fake = RestrictedBot()
    app.bot = fake
    job = Job(
        "photo",
        -100,
        None,
        42,
        7,
        "https://example.com/photo",
        "https://example.com/photo",
        "User",
        True,
    )
    app._set_status_reaction(job, "👀")
    flight = app.coordinator.submit(job)
    assert flight is not None
    metadata = MediaMetadata(
        url=job.url,
        info={"id": "photo", "extractor": "Generic", "formats": []},
        media_key="generic:photo",
        source_name="Example",
    )
    monkeypatch.setattr(main, "validate_public_url", lambda url: url)
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: metadata)

    app._process_flight(flight)

    assert [item[2] for item in fake.reactions] == ["👀", None]
    assert fake.sends == []
    assert fake.deletes == []


def test_short_language_command_changes_group_language(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    monkeypatch.setattr(app, "_is_admin", lambda *_args: True)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100, type="supergroup"),
        from_user=SimpleNamespace(id=7),
    )

    app._handle_language_command(message, "ru")

    assert app.storage.chat_language(-100) == "ru"
    assert "русский" in fake.messages[-1][1]


def test_telegram_commands_offer_only_short_language_switches(tmp_path) -> None:
    class CommandBot:
        def set_my_commands(self, commands) -> None:
            self.commands = commands

    app = main.BotApplication(_settings(tmp_path))
    fake = CommandBot()
    app.bot = fake

    app._set_commands()

    names = [command.command for command in fake.commands]
    assert "audio" in names
    assert "skip" in names
    assert "en" in names
    assert "ru" in names
    assert "language" not in names


def _video_metadata(url: str) -> MediaMetadata:
    return MediaMetadata(
        url=url,
        info={
            "id": "video",
            "extractor": "Test",
            "formats": [{"format_id": "video", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a"}],
        },
        media_key="test:video",
        source_name="Test",
    )


def test_uploads_wait_for_the_configured_timeout_but_file_id_sends_do_not(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(replace(_settings(tmp_path), upload_timeout=420))
    fake = FakeBot()
    app.bot = fake
    url = "https://example.com/video"
    flight = None
    for index in range(2):
        job = Job(str(index), -(index + 1), None, index + 10, index + 20, url, url, "User", True)
        flight = app.coordinator.submit(job) or flight
    assert flight is not None
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    monkeypatch.setattr(main, "validate_public_url", lambda value: value)
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: _video_metadata(url))
    monkeypatch.setattr(app, "_obtain_file", lambda *_args: video_path)

    app._process_flight(flight)

    assert fake.sends[0][1]["timeout"] == 420
    assert fake.sends[1][0] == "telegram-file-id"
    assert "timeout" not in fake.sends[1][1]


def test_upload_timeout_is_reported_as_unconfirmed_and_stays_retryable(tmp_path, monkeypatch, caplog) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()

    def timed_out_upload(*, video, **kwargs):
        fake.sends.append((video, kwargs))
        raise main.requests.exceptions.ReadTimeout("Read timed out. (read timeout=300)")

    fake.send_video = timed_out_upload
    app.bot = fake
    url = "https://example.com/video"
    job = Job("slow", -100, None, 42, 7, url, url, "User", True)
    flight = app.coordinator.submit(job)
    assert flight is not None
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    monkeypatch.setattr(main, "validate_public_url", lambda value: value)
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: _video_metadata(url))
    monkeypatch.setattr(app, "_obtain_file", lambda *_args: video_path)

    with caplog.at_level("WARNING", logger="link_downloader_bot.app"):
        app._process_flight(flight)

    assert fake.deletes == []
    assert [item[2] for item in fake.reactions] == ["👎"]
    assert "delivery unconfirmed job_id=slow" in caplog.text
    assert "Traceback" not in caplog.text
    assert app._claim_failed_retry(_reaction_update("👎")) is not None


def _group_link_message(**extra) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=-100, type="supergroup"),
        from_user=SimpleNamespace(id=7, is_bot=False, first_name="User", last_name="", username="user"),
        text="https://example.com/video",
        caption=None,
        message_id=42,
        **extra,
    )


def _queued_job(app: main.BotApplication, message: SimpleNamespace) -> Job:
    app._handle_group_message(message)
    flight = app.queue.get_nowait()
    assert flight is not None
    job = flight.jobs[0]
    app.coordinator.abort(flight)
    return job


def test_link_posted_as_reply_remembers_the_replied_message(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    app.bot = FakeBot()
    monkeypatch.setattr(main, "validate_public_url", lambda url: url)

    replied = _queued_job(app, _group_link_message(reply_to_message=SimpleNamespace(message_id=30)))
    plain = _queued_job(app, _group_link_message())

    assert replied.reply_to_message_id == 30
    assert plain.reply_to_message_id is None


def test_forum_topic_root_is_not_treated_as_a_reply(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    app.bot = FakeBot()
    monkeypatch.setattr(main, "validate_public_url", lambda url: url)
    topic_root = SimpleNamespace(message_id=5, forum_topic_created=SimpleNamespace(name="Videos"))

    in_topic = _queued_job(
        app,
        _group_link_message(is_topic_message=True, message_thread_id=5, reply_to_message=topic_root),
    )
    replied_in_topic = _queued_job(
        app,
        _group_link_message(
            is_topic_message=True,
            message_thread_id=5,
            reply_to_message=SimpleNamespace(message_id=30, forum_topic_created=None),
        ),
    )

    assert in_topic.message_thread_id == 5
    assert in_topic.reply_to_message_id is None
    assert replied_in_topic.message_thread_id == 5
    assert replied_in_topic.reply_to_message_id == 30


def test_every_delivery_answers_its_own_reply_target(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    url = "https://example.com/video"
    jobs = [
        Job("upload", -100, None, 42, 7, url, url, "User", True, reply_to_message_id=30),
        Job("cached", -200, 9, 43, 8, url, url, "User", True, reply_to_message_id=31),
        Job("plain", -300, None, 44, 9, url, url, "User", True),
    ]
    flight = None
    for job in jobs:
        flight = app.coordinator.submit(job) or flight
    assert flight is not None
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    monkeypatch.setattr(main, "validate_public_url", lambda value: value)
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: _video_metadata(url))
    monkeypatch.setattr(app, "_obtain_file", lambda *_args: video_path)

    app._process_flight(flight)

    upload, cached, plain = (kwargs for _video, kwargs in fake.sends)
    assert upload["reply_parameters"].message_id == 30
    assert upload["reply_parameters"].allow_sending_without_reply is True
    assert cached["reply_parameters"].message_id == 31
    assert cached["message_thread_id"] == 9
    assert "reply_parameters" not in plain
    assert fake.deletes == [(-100, 42), (-200, 43), (-300, 44)]


def test_audio_delivery_answers_the_reply_target(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    url = "https://example.com/video"
    job = Job("audio", -100, None, 42, 7, url, url, "User", True, media_kind="audio", reply_to_message_id=30)

    app._send_media(job, "telegram-audio-file-id", _video_metadata(url), upload=False)

    assert fake.audio_sends[0][1]["reply_parameters"].message_id == 30


def test_reaction_retry_keeps_the_reply_target(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    app.bot = FakeBot()
    url = "https://example.com/video"
    app._after_failure(Job("failed", -100, None, 42, 7, url, url, "User", True, reply_to_message_id=30))

    app._handle_retry_reaction(_reaction_update("👎"))

    flight = app.queue.get_nowait()
    assert flight is not None
    assert flight.jobs[0].job_id != "failed"
    assert flight.jobs[0].reply_to_message_id == 30
    app.coordinator.abort(flight)


def _sender_reaction(emoji: str = "👀", *, user_id: int = 7, old: tuple[str, ...] = ()) -> SimpleNamespace:
    return _reaction_update(emoji, user_id=user_id, old=old)


def _accepted_link(app: main.BotApplication, monkeypatch, **extra):
    monkeypatch.setattr(main, "validate_public_url", lambda url: url)
    app._handle_group_message(_group_link_message(**extra))
    flight = app.queue.get_nowait()
    assert flight is not None
    return flight


def _probe_returns_video(monkeypatch) -> None:
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: _video_metadata("https://example.com/video"))


def _write_download(app: main.BotApplication, prefix: str) -> dict:
    (app.settings.output_dir / f"{prefix}.mp4").write_bytes(b"video")
    return {"id": "video"}


def test_sender_eyes_cancel_a_queued_link_before_any_website_request(tmp_path, monkeypatch, caplog) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    flight = _accepted_link(app, monkeypatch)
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: (_ for _ in ()).throw(AssertionError("probed")))

    with caplog.at_level("INFO", logger="link_downloader_bot.app"):
        app._handle_reaction(_sender_reaction())
    app._process_flight(flight)

    assert [item[2] for item in fake.reactions] == ["👀", None]
    assert fake.sends == []
    assert fake.deletes == []
    assert "job cancelled by sender" in caplog.text


def test_eyes_from_anyone_but_the_sender_do_not_cancel(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    flight = _accepted_link(app, monkeypatch)
    bot_update = _sender_reaction()
    bot_update.user.is_bot = True

    app._handle_reaction(_sender_reaction(user_id=99))
    app._handle_reaction(bot_update)
    app._handle_reaction(_sender_reaction("👍"))

    assert [item[2] for item in fake.reactions] == ["👀"]
    assert not app.coordinator.is_cancelled(flight.jobs[0])
    app.coordinator.abort(flight)


def test_cancel_during_probe_leaves_no_failure_reaction(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    flight = _accepted_link(app, monkeypatch)

    def probe_then_fail(*_args):
        app._handle_reaction(_sender_reaction())
        raise RuntimeError("extractor failed")

    monkeypatch.setattr(main, "extract_metadata", probe_then_fail)

    app._process_flight(flight)

    assert [item[2] for item in fake.reactions] == ["👀", None]
    assert app._claim_failed_retry(_reaction_update("👎")) is None


def test_cancel_after_probe_skips_the_download(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    flight = _accepted_link(app, monkeypatch)

    def probe_then_cancel(*_args):
        app._handle_reaction(_sender_reaction())
        return _video_metadata("https://example.com/video")

    monkeypatch.setattr(main, "extract_metadata", probe_then_cancel)
    monkeypatch.setattr(main, "download_metadata", lambda *_args, **_kwargs: pytest.fail("downloaded"))

    app._process_flight(flight)

    assert fake.sends == []
    assert fake.deletes == []
    assert [item[2] for item in fake.reactions] == ["👀", None]


def test_cancel_during_download_stops_it_and_keeps_the_link(tmp_path, monkeypatch, caplog) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    flight = _accepted_link(app, monkeypatch)
    _probe_returns_video(monkeypatch)

    def download(_metadata, _prefix, _folder, *, should_stop, **_kwargs):
        assert not should_stop()
        app._handle_reaction(_sender_reaction())
        assert should_stop()
        raise DownloadCancelled("download is no longer wanted")

    monkeypatch.setattr(main, "download_metadata", download)

    with caplog.at_level("INFO", logger="link_downloader_bot.app"):
        app._process_flight(flight)

    assert fake.sends == []
    assert fake.deletes == []
    assert [item[2] for item in fake.reactions] == ["👀", None]
    assert "download stopped because every request was cancelled" in caplog.text
    assert app.coordinator.jobs_for_chat(-100) == []


def test_one_sender_cancelling_a_shared_download_does_not_stop_it_for_others(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    url = "https://example.com/video"
    cancelled = Job("cancelled", -100, None, 42, 7, url, url, "User", True)
    kept = Job("kept", -200, None, 43, 8, url, url, "Other", True)
    flight = app.coordinator.submit(cancelled)
    assert flight is not None
    assert app.coordinator.submit(kept) is None
    _probe_returns_video(monkeypatch)

    def download(_metadata, prefix, _folder, *, should_stop, **_kwargs):
        app._handle_reaction(_sender_reaction())
        assert not should_stop()
        return _write_download(app, prefix)

    monkeypatch.setattr(main, "download_metadata", download)

    app._process_flight(flight)

    assert [kwargs["chat_id"] for _video, kwargs in fake.sends] == [-200]
    assert fake.deletes == [(-200, 43)]


def test_link_joining_a_cancelled_download_keeps_it_running(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    url = "https://example.com/video"
    flight = app.coordinator.submit(Job("cancelled", -100, None, 42, 7, url, url, "User", True))
    assert flight is not None
    _probe_returns_video(monkeypatch)

    def download(_metadata, prefix, _folder, *, should_stop, **_kwargs):
        app._handle_reaction(_sender_reaction())
        assert should_stop()
        assert app.coordinator.submit(Job("joined", -200, None, 43, 8, url, url, "Other", True)) is None
        assert not should_stop()
        return _write_download(app, prefix)

    monkeypatch.setattr(main, "download_metadata", download)

    app._process_flight(flight)

    assert [kwargs["chat_id"] for _video, kwargs in fake.sends] == [-200]
    assert fake.deletes == [(-200, 43)]


def test_cancel_during_upload_withdraws_the_posted_video_and_keeps_the_link(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()

    def upload_while_sender_cancels(*, video, **kwargs):
        fake.sends.append((video, kwargs))
        app._handle_reaction(_sender_reaction())
        return SimpleNamespace(message_id=500, video=SimpleNamespace(file_id="telegram-file-id"))

    fake.send_video = upload_while_sender_cancels
    app.bot = fake
    flight = _accepted_link(app, monkeypatch)
    job = flight.jobs[0]
    _probe_returns_video(monkeypatch)
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    monkeypatch.setattr(app, "_obtain_file", lambda *_args: video_path)

    app._process_flight(flight)

    assert len(fake.sends) == 1
    assert fake.deletes == [(-100, 500)]
    assert [item[2] for item in fake.reactions] == ["👀", None]
    profile = main._media_cache_profile(job.url, job.media_kind, app.settings.max_filesize)
    # The upload still feeds the cache, so the next request for this link is instant.
    assert app.storage.get_cached_by_url(f"{job.url_key}|{profile}", 30) is not None


def test_cancelled_job_in_a_shared_flight_gets_no_cached_copy(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    url = "https://example.com/video"
    first = Job("first", -200, None, 43, 8, url, url, "Other", True)
    second = Job("second", -100, None, 42, 7, url, url, "User", True)

    def upload_while_second_sender_cancels(*, video, **kwargs):
        fake.sends.append((video, kwargs))
        if kwargs["chat_id"] == -200:
            app._handle_reaction(_sender_reaction())
        return SimpleNamespace(message_id=500, video=SimpleNamespace(file_id="telegram-file-id"))

    fake.send_video = upload_while_second_sender_cancels
    app.bot = fake
    flight = app.coordinator.submit(first)
    assert flight is not None
    assert app.coordinator.submit(second) is None
    _probe_returns_video(monkeypatch)
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    monkeypatch.setattr(main, "validate_public_url", lambda value: value)
    monkeypatch.setattr(app, "_obtain_file", lambda *_args: video_path)

    app._process_flight(flight)

    assert [kwargs["chat_id"] for _video, kwargs in fake.sends] == [-200]
    assert fake.deletes == [(-200, 43)]


def test_eyes_after_delivery_change_nothing(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(replace(_settings(tmp_path), delete_original=False))
    fake = FakeBot()
    app.bot = fake
    flight = _accepted_link(app, monkeypatch)
    job = flight.jobs[0]

    app._after_success(job, SimpleNamespace(message_id=500))
    app._handle_reaction(_sender_reaction())
    app.coordinator.abort(flight)
    app._handle_reaction(_sender_reaction(old=("👀",)))

    assert [item[2] for item in fake.reactions] == ["👀", "👍"]
    assert fake.deletes == []
    assert not app.coordinator.is_cancelled(job)


def test_status_reaction_racing_a_cancel_never_outlives_it(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    flight = _accepted_link(app, monkeypatch)
    job = flight.jobs[0]
    original_set = fake.set_message_reaction

    def cancel_while_reacting(chat_id, message_id, reaction, **kwargs):
        result = original_set(chat_id, message_id, reaction, **kwargs)
        if reaction and reaction[0].emoji == "🙈":
            app.coordinator.cancel(-100, 42, 7)
        return result

    fake.set_message_reaction = cancel_while_reacting

    app._after_instagram_restriction(job)

    assert [item[2] for item in fake.reactions] == ["👀", "🙈", None]
    assert app._claim_failed_retry(_reaction_update("🙈")) is None
    app.coordinator.abort(flight)


def test_sender_can_cancel_a_reaction_retry(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    url = "https://example.com/video"
    app._after_failure(Job("failed", -100, None, 42, 7, url, url, "User", True))
    app._handle_reaction(_reaction_update("👎", user_id=7))
    flight = app.queue.get_nowait()
    assert flight is not None

    app._handle_reaction(_sender_reaction(old=("👎",)))
    app._process_flight(flight)
    app._handle_reaction(_reaction_update("👎", user_id=99))

    assert [item[2] for item in fake.reactions] == ["👎", "👀", None]
    assert app.queue.empty()


def test_cancel_works_without_status_reactions(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(replace(_settings(tmp_path), status_reactions=False))
    fake = FakeBot()
    app.bot = fake
    flight = _accepted_link(app, monkeypatch)
    monkeypatch.setattr(main, "extract_metadata", lambda *_args: (_ for _ in ()).throw(AssertionError("probed")))

    app._handle_reaction(_sender_reaction())
    app._process_flight(flight)

    assert fake.reactions == []
    assert fake.sends == []
    assert fake.deletes == []


def test_paused_group_ignores_cancel_reactions(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    fake = FakeBot()
    app.bot = fake
    app.group_registry.record_bootstrap_result(-100, title="Paused", chat_type="supergroup", telegram_status="member")
    flight = _accepted_link(app, monkeypatch)
    result, _group = app.group_registry.set_runtime_mode(-100, "soft", 42, expected_revision=0)
    assert result == "changed"
    reactions_before = list(fake.reactions)

    app._handle_reaction(_sender_reaction())

    assert fake.reactions == reactions_before
    assert not app.coordinator.is_cancelled(flight.jobs[0])
    app.coordinator.abort(flight)


class PromptBot(FakeBot):
    def __init__(self) -> None:
        super().__init__()
        self.next_message_id = 500
        self.edits = []
        self.answers = []

    def send_message(self, chat_id, text, **kwargs):
        self.next_message_id += 1
        self.messages.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=self.next_message_id)

    def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))
        return True

    def answer_callback_query(self, callback_id, **kwargs):
        self.answers.append((callback_id, kwargs))
        return True


def _prompt_app(tmp_path) -> tuple[main.BotApplication, PromptBot]:
    app = main.BotApplication(_settings(tmp_path))
    fake = PromptBot()
    app.bot = fake
    app.bot_username = "downloader"
    return app, fake


def _mention(text: str = "@Downloader", *, user_id: int = 7, username: str | None = "alice", **extra):
    return SimpleNamespace(
        chat=SimpleNamespace(id=-100, type="supergroup"),
        from_user=SimpleNamespace(id=user_id, is_bot=False, first_name="Alice", last_name="", username=username),
        text=text,
        caption=None,
        message_id=42,
        **extra,
    )


def _press(message_id: int, data: str, *, user_id: int = 7) -> SimpleNamespace:
    return SimpleNamespace(
        id="callback",
        data=data,
        from_user=SimpleNamespace(id=user_id, is_bot=False),
        message=SimpleNamespace(chat=SimpleNamespace(id=-100, type="supergroup"), message_id=message_id),
    )


def _buttons(kwargs: dict) -> list[tuple[str, str]]:
    return [(button.text, button.callback_data) for row in kwargs["reply_markup"].keyboard for button in row]


def test_bare_mention_asks_to_turn_auto_download_off(tmp_path, monkeypatch) -> None:
    app, fake = _prompt_app(tmp_path)
    scheduled = []
    monkeypatch.setattr(app.janitor, "schedule", lambda *args: scheduled.append(args))

    app._handle_group_message(_mention("  @Downloader  ", message_thread_id=5))

    [(chat_id, text, kwargs)] = fake.messages
    assert chat_id == -100
    assert text == "@alice, turn off auto-download for your links?"
    assert _buttons(kwargs) == [("Yes", "ad:7:off"), ("No", "ad:7:no")]
    assert kwargs["message_thread_id"] == 5
    assert kwargs["parse_mode"] == "HTML"
    assert scheduled == [(-100, 501, main.AUTO_DOWNLOAD_QUESTION_TTL_SECONDS)]
    assert fake.deletes == [(-100, 42)]
    assert app.queue.empty()
    assert fake.reactions == []


def test_opted_out_member_is_asked_to_turn_it_back_on_in_the_group_language(tmp_path) -> None:
    app, fake = _prompt_app(tmp_path)
    app.storage.set_chat_language(-100, "ru")
    app.storage.set_opt_out(-100, 7, True)

    app._handle_group_message(_mention())

    [(_chat_id, text, kwargs)] = fake.messages
    assert text == "@alice, снова включить автоскачивание ваших ссылок?"
    assert _buttons(kwargs) == [("Да", "ad:7:on"), ("Нет", "ad:7:no")]


def test_member_without_username_is_named_with_a_safe_profile_link(tmp_path) -> None:
    app, fake = _prompt_app(tmp_path)
    message = _mention(username=None)
    message.from_user.first_name = "<Bob>"

    app._handle_group_message(message)

    assert fake.messages[0][1] == '<a href="tg://user?id=7">&lt;Bob&gt;</a>, turn off auto-download for your links?'


def test_mention_with_any_words_is_ignored_and_old_toggle_words_do_nothing(tmp_path) -> None:
    app, fake = _prompt_app(tmp_path)
    for text in ("@Downloader me", "@Downloader я", "@Downloader @alice", "hi @Downloader", "@Downloader thanks"):
        app._handle_group_message(_mention(text))

    caption = _mention()
    caption.text = None
    caption.caption = "@Downloader"
    app._handle_group_message(caption)

    assert fake.messages == []
    assert fake.deletes == []
    assert not app.storage.is_opted_out(-100, 7)


def test_mention_with_a_link_downloads_even_while_auto_download_is_off(tmp_path, monkeypatch) -> None:
    app, fake = _prompt_app(tmp_path)
    app.storage.set_opt_out(-100, 7, True)
    monkeypatch.setattr(main, "validate_public_url", lambda url: url)

    app._handle_group_message(_mention("@Downloader https://example.com/video"))
    app._handle_group_message(_mention("https://example.com/other"))

    flight = app.queue.get_nowait()
    assert flight is not None
    assert [job.url for job in flight.jobs] == ["https://example.com/video"]
    assert app.queue.empty()
    assert fake.messages == []
    app.coordinator.abort(flight)


def test_yes_turns_auto_download_off_and_the_answer_disappears_soon(tmp_path, monkeypatch) -> None:
    app, fake = _prompt_app(tmp_path)
    app._handle_group_message(_mention())
    scheduled = []
    monkeypatch.setattr(app.janitor, "schedule", lambda *args: scheduled.append(args))

    app._handle_auto_download_callback(_press(501, "ad:7:off"))

    assert app.storage.is_opted_out(-100, 7)
    assert fake.edits == [
        (
            "Done: auto-download for your links is off.",
            {"chat_id": -100, "message_id": 501, "reply_markup": None},
        )
    ]
    assert scheduled == [(-100, 501, main.AUTO_DOWNLOAD_DONE_TTL_SECONDS)]
    assert fake.answers == [("callback", {"text": "", "show_alert": False})]


def test_yes_sets_the_asked_state_so_repeated_or_late_presses_never_flip_it(tmp_path) -> None:
    app, _fake = _prompt_app(tmp_path)

    app._handle_auto_download_callback(_press(501, "ad:7:off"))
    app._handle_auto_download_callback(_press(501, "ad:7:off"))
    assert app.storage.is_opted_out(-100, 7)

    app._handle_auto_download_callback(_press(502, "ad:7:on"))
    app._handle_auto_download_callback(_press(502, "ad:7:on"))
    assert not app.storage.is_opted_out(-100, 7)


def test_no_removes_the_question_and_changes_nothing(tmp_path) -> None:
    app, fake = _prompt_app(tmp_path)
    app._handle_group_message(_mention())

    app._handle_auto_download_callback(_press(501, "ad:7:no"))

    assert not app.storage.is_opted_out(-100, 7)
    assert fake.deletes == [(-100, 42), (-100, 501)]
    assert fake.edits == []


def test_only_the_asked_member_can_answer(tmp_path) -> None:
    app, fake = _prompt_app(tmp_path)
    app.storage.set_chat_language(-100, "ru")
    app._handle_group_message(_mention())

    app._handle_auto_download_callback(_press(501, "ad:7:off", user_id=8))

    assert not app.storage.is_opted_out(-100, 7)
    assert not app.storage.is_opted_out(-100, 8)
    assert fake.answers == [("callback", {"text": "Этот вопрос не для вас.", "show_alert": True})]
    assert fake.edits == []
    assert fake.deletes == [(-100, 42)]


def test_new_question_replaces_the_previous_one(tmp_path) -> None:
    app, fake = _prompt_app(tmp_path)

    app._handle_group_message(_mention())
    app._handle_group_message(_mention())

    assert fake.deletes == [(-100, 42), (-100, 501), (-100, 42)]


def test_unanswered_question_is_removed_after_its_lifetime(tmp_path, monkeypatch) -> None:
    app, fake = _prompt_app(tmp_path)
    now = [1_000.0]
    monkeypatch.setattr(app.janitor, "_clock", lambda: now[0])
    app._handle_group_message(_mention())

    now[0] += main.AUTO_DOWNLOAD_QUESTION_TTL_SECONDS - 1
    app.janitor.run_due()
    assert (-100, 501) not in fake.deletes

    now[0] += 1
    app.janitor.run_due()
    assert (-100, 501) in fake.deletes


def test_malformed_or_foreign_presses_change_nothing(tmp_path) -> None:
    app, fake = _prompt_app(tmp_path)
    presses = [_press(501, "ad:7:maybe"), _press(501, "ad:x:off"), _press(501, "ad:7")]
    private = _press(501, "ad:7:off")
    private.message.chat.type = "private"
    presses.append(private)

    for press in presses:
        app._handle_auto_download_callback(press)

    assert not app.storage.is_opted_out(-100, 7)
    assert fake.edits == []
    assert len(fake.answers) == len(presses)


def test_paused_group_ignores_bare_mentions_and_answers(tmp_path) -> None:
    app, fake = _prompt_app(tmp_path)
    app.group_registry.record_bootstrap_result(-100, title="Paused", chat_type="supergroup", telegram_status="member")
    result, _group = app.group_registry.set_runtime_mode(-100, "soft", 42, expected_revision=0)
    assert result == "changed"

    app._handle_group_message(_mention())
    app._handle_auto_download_callback(_press(501, "ad:7:off"))

    assert fake.messages == []
    assert not app.storage.is_opted_out(-100, 7)


def test_auto_download_callbacks_have_their_own_handler(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    filters = [handler["filters"]["func"] for handler in app.bot.callback_query_handlers]

    assert [check(SimpleNamespace(data="ad:7:off")) for check in filters].count(True) == 1
