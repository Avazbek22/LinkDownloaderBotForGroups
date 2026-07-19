from __future__ import annotations

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

    def send_video(self, *, video, **kwargs):
        self.sends.append((video, kwargs))
        return SimpleNamespace(video=SimpleNamespace(file_id="telegram-file-id"))

    def delete_message(self, chat_id, message_id):
        self.deletes.append((chat_id, message_id))


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
        info={"id": "video", "extractor": "Test"},
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
