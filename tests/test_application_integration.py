from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import main
from app.download_backend import MediaMetadata
from app.jobs import Job
from app.settings import Settings


class FakeBot:
    def __init__(self) -> None:
        self.sends = []
        self.deletes = []
        self.reactions = []
        self.messages = []

    def send_video(self, *, video, **kwargs):
        self.sends.append((video, kwargs))
        return SimpleNamespace(video=SimpleNamespace(file_id="telegram-file-id"))

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


def test_self_mention_requires_a_message_token() -> None:
    assert main.BotApplication._self_mention("hello @alice", "alice")
    assert not main.BotApplication._self_mention("https://example.com/@alice/video", "alice")


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

    assert [item[2] for item in fake.reactions] == ["👀", None]


def test_reactions_can_be_disabled(tmp_path) -> None:
    app = main.BotApplication(replace(_settings(tmp_path), status_reactions=False))
    fake = FakeBot()
    app.bot = fake
    job = Job("job", -100, None, 42, 7, "https://example.com", "key", "User", True)

    app._set_status_reaction(job, "👀")

    assert fake.reactions == []


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


def test_non_video_metadata_clears_eyes_without_downvote(tmp_path, monkeypatch) -> None:
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
    assert "en" in names
    assert "ru" in names
    assert "language" not in names
