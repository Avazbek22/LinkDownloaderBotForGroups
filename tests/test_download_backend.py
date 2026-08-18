from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app import download_backend
from app.download_backend import (
    FormatPlan,
    InstagramContentRestrictedError,
    MediaMetadata,
    display_source_name,
    download_metadata,
    has_downloadable_video,
    select_format,
    select_format_candidates,
)


def test_instagram_content_restriction_detection_is_specific() -> None:
    audience_restricted = RuntimeError(
        "ERROR: [Instagram] post: This content isn't available to everyone: It can't be seen by certain audiences."
    )
    empty_response = RuntimeError(
        "ERROR: [Instagram] post: Instagram sent an empty media response. "
        "Check if this post is accessible in your browser without being logged-in."
    )

    assert download_backend._is_instagram_content_restriction(audience_restricted)
    assert download_backend._is_instagram_content_restriction(empty_response)
    assert not download_backend._is_instagram_content_restriction(
        RuntimeError("ERROR: [Instagram] post: Requested content is not available, login required")
    )
    assert not download_backend._is_instagram_content_restriction(
        RuntimeError("ERROR: [Example] content is not available to everyone")
    )


def test_instagram_probe_preserves_restriction_across_fallback(monkeypatch) -> None:
    errors = iter(
        [
            RuntimeError(
                "ERROR: [Instagram] post: Instagram sent an empty media response. "
                "Check if this post is accessible in your browser without being logged-in."
            ),
            RuntimeError("generic fallback error"),
        ]
    )

    class FakeYDL:
        def __init__(self, _options: dict) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def extract_info(self, *_args, **_kwargs):
            raise next(errors)

    monkeypatch.setattr(download_backend.yt_dlp, "YoutubeDL", FakeYDL)

    with pytest.raises(InstagramContentRestrictedError):
        download_backend.extract_metadata("https://www.instagram.com/reel/restricted/")


def test_source_names_use_brand_spelling() -> None:
    assert display_source_name("Youtube") == "YouTube"
    assert display_source_name("youtube") == "YouTube"
    assert display_source_name("TikTok") == "TikTok"
    assert display_source_name("VKontakte") == "VK"
    assert display_source_name("CustomExtractor") == "CustomExtractor"


def test_video_metadata_detection_ignores_audio_and_articles() -> None:
    assert has_downloadable_video({"formats": [{"ext": "mp4", "vcodec": "avc1.4d401f", "acodec": "mp4a.40.2"}]})
    assert has_downloadable_video(
        {"formats": [{"ext": "mp4", "vcodec": None, "acodec": None, "url": "https://cdn.example/video.mp4"}]}
    )
    assert not has_downloadable_video({"formats": [{"ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2"}]})
    assert not has_downloadable_video({"id": "article", "title": "News article"})


def test_selects_best_format_that_fits() -> None:
    info = {
        "duration": 60,
        "formats": [
            {
                "format_id": "small",
                "ext": "mp4",
                "vcodec": "avc1.4d401f",
                "acodec": "mp4a.40.2",
                "height": 720,
                "filesize": 20_000_000,
            },
            {
                "format_id": "large",
                "ext": "mp4",
                "vcodec": "avc1.640028",
                "acodec": "mp4a.40.2",
                "height": 1080,
                "filesize": 80_000_000,
            },
        ],
    }
    assert select_format(info, 50_000_000) == ("small", None)


def test_combines_video_and_audio_sizes() -> None:
    info = {
        "formats": [
            {
                "format_id": "v720",
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "none",
                "height": 720,
                "filesize": 30_000_000,
            },
            {
                "format_id": "v1080",
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "none",
                "height": 1080,
                "filesize": 49_000_000,
            },
            {
                "format_id": "audio",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a",
                "filesize": 5_000_000,
            },
        ]
    }
    assert select_format(info, 50_000_000) == ("v720+audio", "mp4")


def test_higher_unknown_format_is_tried_before_lower_known_format() -> None:
    info = {
        "formats": [
            {
                "format_id": "known",
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "height": 720,
                "filesize": 20_000_000,
            },
            {
                "format_id": "unknown",
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "height": 2160,
            },
        ]
    }
    assert select_format(info, 50_000_000) == ("unknown", None)


def test_unknown_direct_instagram_mp4_wins_over_incompatible_dash() -> None:
    info = {
        "formats": [
            {
                "format_id": "direct",
                "ext": "mp4",
                "vcodec": None,
                "acodec": None,
                "url": "https://cdn.example/video.mp4",
            },
            {
                "format_id": "vp9-video",
                "ext": "mp4",
                "vcodec": "vp09.00.40.08",
                "acodec": "none",
                "height": 1920,
                "tbr": 4600,
            },
            {
                "format_id": "audio",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.5",
                "tbr": 70,
            },
        ]
    }

    assert select_format(info, 50_000_000) == ("direct", None)
    assert [plan.format_spec for plan in select_format_candidates(info, 50_000_000)] == ["direct"]


def test_unselect_info_removes_stale_default_without_losing_formats() -> None:
    formats = [{"format_id": "direct"}]
    clean = download_backend._unselect_info(
        {
            "id": "video",
            "webpage_url": "https://example.com/video",
            "formats": formats,
            "format_id": "stale-vp9+audio",
            "requested_formats": [{"format_id": "stale-vp9"}],
            "url": "https://cdn.example/stale.mp4",
            "vcodec": "vp9",
        }
    )

    assert clean["formats"] is formats
    assert clean["webpage_url"] == "https://example.com/video"
    assert "format_id" not in clean
    assert "requested_formats" not in clean
    assert "url" not in clean


def test_video_validation_requires_h264_stream(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"not-a-real-video")

    def probe(codec: str, codec_type: str = "video") -> SimpleNamespace:
        return SimpleNamespace(
            stdout=(
                '{"streams":[{"codec_type":"'
                + codec_type
                + '","codec_name":"'
                + codec
                + '","width":720,"height":1280}],'
                '"format":{"format_name":"mov,mp4,m4a,3gp,3g2,mj2"}}'
            )
        )

    monkeypatch.setattr(download_backend.subprocess, "run", lambda *_args, **_kwargs: probe("h264"))
    download_backend._validate_downloaded_video(path, 1_000_000)

    monkeypatch.setattr(download_backend.subprocess, "run", lambda *_args, **_kwargs: probe("vp9"))
    with pytest.raises(RuntimeError, match="not Telegram-compatible"):
        download_backend._validate_downloaded_video(path, 1_000_000)

    monkeypatch.setattr(
        download_backend.subprocess,
        "run",
        lambda *_args, **_kwargs: probe("aac", codec_type="audio"),
    )
    with pytest.raises(RuntimeError, match="no video stream"):
        download_backend._validate_downloaded_video(path, 1_000_000)


def test_youtube_retry_reextracts_with_runtime_and_selected_format(monkeypatch, tmp_path: Path) -> None:
    options_seen: list[dict] = []

    class FakeYDL:
        def __init__(self, options: dict) -> None:
            self.options = options
            options_seen.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def process_ie_result(self, *_args, **_kwargs):
            raise RuntimeError("expired media URL")

        def extract_info(self, *_args, **_kwargs):
            (tmp_path / "retry.mp4").write_bytes(b"video")
            return {"id": "video", "extractor": "youtube"}

    monkeypatch.setattr(download_backend.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(
        download_backend,
        "_site_options",
        lambda *_args, **_kwargs: {"js_runtimes": {"node": {}}},
    )
    monkeypatch.setattr(download_backend, "_validate_downloaded_video", lambda *_args, **_kwargs: None)
    metadata = MediaMetadata(
        url="https://www.youtube.com/watch?v=video",
        info={
            "formats": [
                {
                    "format_id": "video",
                    "ext": "mp4",
                    "vcodec": "avc1",
                    "acodec": "none",
                    "filesize": 4_000_000,
                },
                {
                    "format_id": "audio",
                    "ext": "m4a",
                    "vcodec": "none",
                    "acodec": "mp4a",
                    "filesize": 1_000_000,
                },
            ]
        },
        media_key="youtube:video",
        source_name="YouTube",
    )

    download_metadata(
        metadata,
        "retry",
        tmp_path,
        max_send_bytes=10_000_000,
        concurrent_fragments=2,
    )

    assert len(options_seen) == 2
    assert options_seen[1]["format"] == "video+audio"
    assert options_seen[1]["merge_output_format"] == "mp4"
    assert options_seen[1]["js_runtimes"] == {"node": {}}


def test_youtube_player_client_is_passed_to_extractor_args(monkeypatch) -> None:
    monkeypatch.setattr(download_backend.env_config, "YTDLP_YOUTUBE_PLAYER_CLIENTS", "android")

    options = download_backend._site_options("https://www.youtube.com/watch?v=video", youtube_player_client="android")

    assert options["extractor_args"] == {"youtube": {"player_client": ["android"]}}
    assert options["js_runtimes"] == {"node": {}}


def test_youtube_client_fallback_chain_and_order(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(download_backend.env_config, "YTDLP_YOUTUBE_PLAYER_CLIENTS", "web,android,ios")
    clients_seen: list[str] = []

    class FakeYDL:
        def __init__(self, options: dict) -> None:
            self.client = (
                options["extractor_args"]["youtube"]["player_client"][0]
                if "extractor_args" in options and "youtube" in options["extractor_args"]
                else "web"
            )
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def process_ie_result(self, *_args, **_kwargs):
            clients_seen.append(f"proc:{self.client}")
            if self.client == "ios":
                (tmp_path / "fallback.mp4").write_bytes(b"video")
                return {"id": "video"}
            raise RuntimeError("expired media URL")

        def extract_info(self, *_args, **_kwargs):
            clients_seen.append(f"extract:{self.client}")
            raise RuntimeError(f"{self.client} blocked")

    monkeypatch.setattr(download_backend.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(download_backend, "_validate_downloaded_video", lambda *_args, **_kwargs: None)

    metadata = MediaMetadata(
        url="https://www.youtube.com/watch?v=video",
        info={
            "formats": [
                {
                    "format_id": "video",
                    "ext": "mp4",
                    "vcodec": "avc1",
                    "acodec": "none",
                    "filesize": 4_000_000,
                },
            ]
        },
        media_key="youtube:video",
        source_name="YouTube",
    )

    download_backend.download_metadata(
        metadata,
        "fallback",
        tmp_path,
        max_send_bytes=10_000_000,
        concurrent_fragments=2,
    )

    assert clients_seen == ["proc:web", "extract:web", "proc:android", "extract:android", "proc:ios"]


def test_rejects_audio_only_attempt_and_uses_next_candidate(monkeypatch, tmp_path: Path) -> None:
    formats_seen: list[str] = []

    class FakeYDL:
        def __init__(self, options: dict) -> None:
            self.options = options
            formats_seen.append(options["format"])

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def process_ie_result(self, *_args, **_kwargs):
            suffix = "m4a" if self.options["format"] == "bad" else "mp4"
            (tmp_path / f"candidate.{suffix}").write_bytes(b"media")
            return {"id": "video"}

    monkeypatch.setattr(download_backend.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(
        download_backend,
        "select_format_candidates",
        lambda *_args, **_kwargs: [FormatPlan("bad"), FormatPlan("good")],
    )

    def validate(path: Path, _max_bytes: int) -> None:
        if path.suffix == ".m4a":
            raise RuntimeError("downloaded file has no video stream")

    monkeypatch.setattr(download_backend, "_validate_downloaded_video", validate)
    metadata = MediaMetadata(
        url="https://example.com/video",
        info={"id": "video", "formats": []},
        media_key="example:video",
        source_name="Example",
    )

    result = download_metadata(
        metadata,
        "candidate",
        tmp_path,
        max_send_bytes=10_000_000,
        concurrent_fragments=2,
    )

    assert result["id"] == "video"
    assert formats_seen == ["bad", "good"]
    assert not (tmp_path / "candidate.m4a").exists()
    assert (tmp_path / "candidate.mp4").exists()
