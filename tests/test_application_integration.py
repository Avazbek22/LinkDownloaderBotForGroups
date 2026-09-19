from __future__ import annotations

import queue
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import main
from app.download_backend import InstagramContentRestrictedError, MediaMetadata, SourceRateLimitedError
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


def test_self_mention_requires_a_message_token() -> None:
    assert main.BotApplication._self_mention("hello @alice", "alice")
    assert not main.BotApplication._self_mention("https://example.com/@alice/video", "alice")


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
    app.storage.toggle_opt_out(-100, 7)
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
