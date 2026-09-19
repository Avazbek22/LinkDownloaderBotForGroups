from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app import download_backend
from app.download_backend import (
    DownloadDeadlineExceeded,
    FormatPlan,
    InstagramContentRestrictedError,
    MediaMetadata,
    SourceRateLimitedError,
    display_source_name,
    download_metadata,
    extract_metadata,
    has_downloadable_audio,
    has_downloadable_video,
    select_audio_format_candidates,
    select_format,
    select_format_candidates,
)
from app.url_utils import is_instagram_url, is_youtube_url


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


def test_rate_limit_detection_is_specific_and_follows_wrapped_errors() -> None:
    instagram_limit = RuntimeError(
        "ERROR: [Instagram] post: You have exceeded the rate-limit for accessing posts anonymously"
    )
    youtube_limit = RuntimeError("ERROR: [youtube] Sign in to confirm you're not a bot")
    wrapped_youtube_limit = RuntimeError("all YouTube metadata clients failed")
    wrapped_youtube_limit.__cause__ = youtube_limit

    assert download_backend._is_source_rate_limit(instagram_limit, "instagram")
    assert download_backend._is_source_rate_limit(wrapped_youtube_limit, "youtube")
    assert not download_backend._is_source_rate_limit(
        RuntimeError("ERROR: [Instagram] Requested content is not available, rate-limit reached or login required"),
        "instagram",
    )
    assert not download_backend._is_source_rate_limit(
        RuntimeError("ERROR: [youtube] Sign in to confirm your age"),
        "youtube",
    )
    assert not download_backend._is_source_rate_limit(
        RuntimeError("ERROR: [youtube] Video https://youtu.be/429 is unavailable"),
        "youtube",
    )


def test_instagram_anonymous_limit_stops_before_fallback_request(monkeypatch) -> None:
    calls = 0

    class FakeYDL:
        def __init__(self, _options: dict) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def extract_info(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise RuntimeError(
                "ERROR: [Instagram] post: You have exceeded the rate-limit for accessing posts anonymously"
            )

    monkeypatch.setattr(download_backend.yt_dlp, "YoutubeDL", FakeYDL)

    with pytest.raises(SourceRateLimitedError, match="Instagram") as raised:
        extract_metadata("https://www.instagram.com/reel/rate-limited/")

    assert raised.value.source == "instagram"
    assert calls == 1


def test_youtube_bot_challenge_stops_client_fallback(monkeypatch) -> None:
    calls = 0

    class FakeYDL:
        def __init__(self, _options: dict) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def extract_info(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise RuntimeError("ERROR: [youtube] Sign in to confirm you're not a bot")

    monkeypatch.setattr(download_backend.env_config, "YTDLP_YOUTUBE_PLAYER_CLIENTS", "default,android,ios")
    monkeypatch.setattr(download_backend.yt_dlp, "YoutubeDL", FakeYDL)

    with pytest.raises(SourceRateLimitedError, match="YouTube") as raised:
        extract_metadata("https://www.youtube.com/watch?v=rate-limited")

    assert raised.value.source == "youtube"
    assert calls == 1


def test_download_rate_limit_stops_format_fallback_and_removes_partial_file(monkeypatch, tmp_path: Path) -> None:
    calls = 0

    class FakeYDL:
        def __init__(self, _options: dict) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def process_ie_result(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            (tmp_path / "limited.part").write_bytes(b"partial")
            raise RuntimeError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(download_backend.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(
        download_backend,
        "select_format_candidates",
        lambda *_args, **_kwargs: [FormatPlan("first"), FormatPlan("second")],
    )
    metadata = MediaMetadata(
        url="https://www.youtube.com/watch?v=limited",
        info={"id": "limited", "formats": []},
        media_key="youtube:limited",
        source_name="YouTube",
    )

    with pytest.raises(SourceRateLimitedError) as raised:
        download_metadata(
            metadata,
            "limited",
            tmp_path,
            max_send_bytes=10_000_000,
            concurrent_fragments=2,
        )

    assert raised.value.source == "youtube"
    assert calls == 1
    assert not (tmp_path / "limited.part").exists()


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
    assert not has_downloadable_video({"formats": None, "entries": None})
    assert select_format_candidates({"formats": None}, 50_000_000)


def test_audio_metadata_detection_accepts_audio_and_unknown_direct_media() -> None:
    assert has_downloadable_audio({"formats": [{"ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2"}]})
    assert has_downloadable_audio(
        {
            "formats": [
                {
                    "ext": "mp4",
                    "vcodec": None,
                    "acodec": None,
                    "url": "https://cdn.example/media.mp4",
                }
            ]
        }
    )
    assert has_downloadable_audio(
        {
            "formats": [
                {
                    "ext": "mp4",
                    "vcodec": "avc1.4d401f",
                    "acodec": None,
                    "url": "https://cdn.example/media.mp4",
                }
            ]
        }
    )
    assert not has_downloadable_audio({"formats": [{"ext": "mp4", "vcodec": "avc1.4d401f", "acodec": "none"}]})
    assert not has_downloadable_audio({"id": "article", "title": "News article"})


def test_audio_plan_chooses_highest_mp3_bitrate_with_headroom() -> None:
    info = {
        "duration": 600,
        "formats": [{"format_id": "audio", "ext": "m4a", "vcodec": "none", "acodec": "mp4a"}],
    }

    plans = select_audio_format_candidates(info, 10_000_000)

    assert plans == [FormatPlan("bestaudio/best", audio_bitrate_kbps=112)]
    assert select_audio_format_candidates({**info, "duration": None}, 10_000_000) == []
    assert select_audio_format_candidates({**info, "duration": 10_000}, 10_000_000) == []


def test_audio_plan_prefers_original_source_over_higher_bitrate_auto_dub() -> None:
    info = {
        "duration": 60,
        "formats": [
            {
                "format_id": "dubbed",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 192,
                "language_preference": 5,
                "format_note": "English - dubbed-auto (default)",
            },
            {
                "format_id": "original",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 128,
                "language_preference": 10,
                "format_note": "Russian - original",
            },
        ],
    }

    plans = select_audio_format_candidates(info, 10_000_000)

    assert [plan.format_spec for plan in plans] == ["original", "dubbed"]
    assert {plan.audio_bitrate_kbps for plan in plans} == {192}


def test_video_plan_prefers_original_audio_over_higher_bitrate_auto_dub() -> None:
    info = {
        "formats": [
            {
                "format_id": "video",
                "ext": "mp4",
                "vcodec": "avc1.640028",
                "acodec": "none",
                "height": 1080,
                "filesize": 30_000_000,
            },
            {
                "format_id": "dubbed",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 192,
                "filesize": 5_000_000,
                "language": "en-US",
                "language_preference": 5,
                "format_note": "English - dubbed-auto (default)",
            },
            {
                "format_id": "original",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 128,
                "filesize": 5_000_000,
                "language": "ru",
                "language_preference": 10,
                "format_note": "Russian - original",
            },
        ]
    }

    plans = select_format_candidates(info, 50_000_000)

    assert [plan.format_spec for plan in plans[:2]] == ["video+original", "video+dubbed"]


def test_caption_language_identifies_original_when_youtube_does_not_label_track() -> None:
    info = {
        "duration": 60,
        "automatic_captions": {
            "ru-orig": [{"name": "Russian (Original)"}],
            "en": [{"name": "English"}],
        },
        "formats": [
            {
                "format_id": "video",
                "ext": "mp4",
                "vcodec": "avc1.640028",
                "acodec": "none",
                "height": 1080,
                "filesize": 30_000_000,
            },
            {
                "format_id": "regional-default",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 192,
                "filesize": 5_000_000,
                "language": "en-US",
                "language_preference": 5,
                "format_note": "English (United States) (default)",
            },
            {
                "format_id": "source-language",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 128,
                "filesize": 5_000_000,
                "language": "ru",
                "language_preference": -1,
                "format_note": "Russian",
            },
        ],
    }

    assert select_format(info, 50_000_000) == ("video+source-language", "mp4")
    assert [plan.format_spec for plan in select_audio_format_candidates(info, 50_000_000)[:2]] == [
        "source-language",
        "regional-default",
    ]


def test_original_progressive_audio_wins_over_higher_resolution_dub() -> None:
    info = {
        "formats": [
            {
                "format_id": "dubbed-720",
                "ext": "mp4",
                "vcodec": "avc1.64001f",
                "acodec": "mp4a.40.2",
                "height": 720,
                "tbr": 2_000,
                "filesize": 25_000_000,
                "format_note": "English - auto-dubbed (default)",
            },
            {
                "format_id": "original-360",
                "ext": "mp4",
                "vcodec": "avc1.42001e",
                "acodec": "mp4a.40.2",
                "height": 360,
                "tbr": 500,
                "filesize": 8_000_000,
                "format_note": "Russian - original",
            },
        ]
    }

    assert select_format(info, 50_000_000) == ("original-360", None)


def test_video_plan_uses_dub_when_original_cannot_fit() -> None:
    info = {
        "formats": [
            {
                "format_id": "video",
                "ext": "mp4",
                "vcodec": "avc1.640028",
                "acodec": "none",
                "height": 1080,
                "filesize": 43_000_000,
            },
            {
                "format_id": "original-too-large",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "filesize": 8_000_000,
                "language_preference": 10,
            },
            {
                "format_id": "dubbed-fallback",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "filesize": 4_000_000,
                "format_note": "English - dubbed-auto (default)",
            },
        ]
    }

    assert select_format(info, 50_000_000) == ("video+dubbed-fallback", "mp4")


def test_site_detection_accepts_explicit_ports_and_trailing_dot() -> None:
    assert is_youtube_url("https://youtube.com:443/watch?v=video")
    assert is_youtube_url("https://www.youtube.com./watch?v=video")
    assert is_instagram_url("https://instagram.com:443/reel/video")


def test_format_ranking_tolerates_malformed_numeric_metadata() -> None:
    info = {
        "formats": [
            {
                "format_id": "video",
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "height": "unknown",
                "fps": {},
                "tbr": [],
                "filesize": 1_000_000,
            }
        ]
    }

    assert select_format(info, 50_000_000) == ("video", None)


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


def test_skips_pair_when_one_known_component_already_exceeds_budget() -> None:
    info = {
        "formats": [
            {
                "format_id": "oversized-video",
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "none",
                "filesize": 60_000_000,
            },
            {
                "format_id": "unknown-audio",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a",
            },
        ]
    }

    plans = select_format_candidates(info, 50_000_000)

    assert all(plan.format_spec != "oversized-video+unknown-audio" for plan in plans)


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


def test_audio_validation_requires_mp3_stream_and_container(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"not-a-real-audio-file")

    def probe(codec: str, format_name: str = "mp3") -> SimpleNamespace:
        return SimpleNamespace(
            stdout=(
                '{"streams":[{"codec_type":"audio","codec_name":"'
                + codec
                + '"}],"format":{"format_name":"'
                + format_name
                + '"}}'
            )
        )

    monkeypatch.setattr(download_backend.subprocess, "run", lambda *_args, **_kwargs: probe("mp3"))
    download_backend._validate_downloaded_audio(path, 1_000_000)

    monkeypatch.setattr(download_backend.subprocess, "run", lambda *_args, **_kwargs: probe("aac", "mov,mp4,m4a"))
    with pytest.raises(RuntimeError, match="codec is not Telegram-compatible"):
        download_backend._validate_downloaded_audio(path, 1_000_000)

    monkeypatch.setattr(download_backend.subprocess, "run", lambda *_args, **_kwargs: probe("mp3", "matroska"))
    with pytest.raises(RuntimeError, match="container is not MP3-compatible"):
        download_backend._validate_downloaded_audio(path, 1_000_000)


def test_youtube_retry_reextracts_with_runtime_and_selected_format(monkeypatch, tmp_path: Path) -> None:
    options_seen: list[dict] = []
    process_calls = 0

    class FakeYDL:
        def __init__(self, options: dict) -> None:
            self.options = options
            options_seen.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def process_ie_result(self, *_args, **_kwargs):
            nonlocal process_calls
            process_calls += 1
            if process_calls == 1:
                raise RuntimeError("expired media URL")
            (tmp_path / "retry.mp4").write_bytes(b"video")
            return {"id": "video", "extractor": "youtube"}

        def extract_info(self, *_args, **_kwargs):
            return {
                "id": "video",
                "extractor": "youtube",
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
                ],
            }

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

    assert len(options_seen) == 3
    assert "format" not in options_seen[1]
    assert options_seen[2]["format"] == "video+audio"
    assert options_seen[2]["merge_output_format"] == "mp4"
    assert options_seen[2]["js_runtimes"] == {"node": {}}


def test_youtube_player_client_is_passed_to_extractor_args(monkeypatch) -> None:
    monkeypatch.setattr(download_backend.env_config, "YTDLP_YOUTUBE_PLAYER_CLIENTS", "android")

    options = download_backend._site_options("https://www.youtube.com/watch?v=video", youtube_player_client="android")

    assert options["extractor_args"] == {"youtube": {"player_client": ["android"]}}
    assert options["js_runtimes"] == {"node": {}}
    assert options["format_sort"] == ["lang"]
    assert options["format_sort_force"] is True

    localized = download_backend._site_options(
        "https://www.youtube.com/watch?v=video",
        youtube_player_client="android",
        youtube_language="ru",
    )
    assert localized["extractor_args"] == {"youtube": {"player_client": ["android"], "lang": ["ru"]}}


def test_youtube_client_configuration_preserves_web_alias_and_legacy(monkeypatch) -> None:
    monkeypatch.setattr(download_backend.env_config, "YTDLP_YOUTUBE_PLAYER_CLIENTS", "default,web,ios")
    monkeypatch.setattr(download_backend.env_config, "YTDLP_YOUTUBE_PLAYER_CLIENT", "")
    assert download_backend._youtube_player_clients() == [None, "ios"]

    monkeypatch.setattr(download_backend.env_config, "YTDLP_YOUTUBE_PLAYER_CLIENTS", "")
    monkeypatch.setattr(download_backend.env_config, "YTDLP_YOUTUBE_PLAYER_CLIENT", "android")
    assert download_backend._youtube_player_clients() == ["android"]

    monkeypatch.setattr(download_backend.env_config, "YTDLP_YOUTUBE_PLAYER_CLIENT", "")
    assert download_backend._youtube_player_clients() == [None, "android", "ios"]


def test_youtube_metadata_uses_client_fallback(monkeypatch) -> None:
    clients_seen: list[str] = []

    class FakeYDL:
        def __init__(self, options: dict) -> None:
            self.client = options.get("extractor_args", {}).get("youtube", {}).get("player_client", ["default"])[0]

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def extract_info(self, *_args, **_kwargs):
            clients_seen.append(self.client)
            if self.client == "default":
                raise RuntimeError("default blocked")
            return {
                "id": "video",
                "extractor_key": "Youtube",
                "formats": [{"format_id": "18", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a"}],
            }

    monkeypatch.setattr(download_backend.env_config, "YTDLP_YOUTUBE_PLAYER_CLIENTS", "default,android,ios")
    monkeypatch.setattr(download_backend.yt_dlp, "YoutubeDL", FakeYDL)

    metadata = extract_metadata("https://www.youtube.com/watch?v=video")

    assert metadata.media_key == "youtube:video"
    assert clients_seen == ["default", "android"]


def test_youtube_metadata_reextracts_regional_dub_in_original_caption_language(monkeypatch) -> None:
    languages_seen: list[str | None] = []

    class FakeYDL:
        def __init__(self, options: dict) -> None:
            language = options.get("extractor_args", {}).get("youtube", {}).get("lang", [None])[0]
            self.language = language

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def extract_info(self, *_args, **_kwargs):
            languages_seen.append(self.language)
            language = self.language or "en-US"
            return {
                "id": "video",
                "extractor_key": "Youtube",
                "automatic_captions": {"ru-orig": [{"name": "Russian (Original)"}]},
                "formats": [
                    {
                        "format_id": "18",
                        "ext": "mp4",
                        "vcodec": "avc1",
                        "acodec": "mp4a",
                        "language": language,
                        "language_preference": -1 if self.language else 5,
                    }
                ],
            }

    monkeypatch.setattr(download_backend.env_config, "YTDLP_YOUTUBE_PLAYER_CLIENTS", "default")
    monkeypatch.setattr(download_backend.yt_dlp, "YoutubeDL", FakeYDL)

    metadata = extract_metadata("https://www.youtube.com/watch?v=video")

    assert languages_seen == [None, "ru"]
    assert metadata.original_audio_language == "ru"
    assert metadata.info["formats"][0]["language"] == "ru"


def test_youtube_metadata_keeps_original_track_without_extra_request(monkeypatch) -> None:
    calls = 0

    class FakeYDL:
        def __init__(self, _options: dict) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def extract_info(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return {
                "id": "video",
                "extractor_key": "Youtube",
                "automatic_captions": {"ru-orig": [{"name": "Russian (Original)"}]},
                "formats": [
                    {
                        "format_id": "18",
                        "ext": "mp4",
                        "vcodec": "avc1",
                        "acodec": "mp4a",
                        "language": "ru",
                        "language_preference": -1,
                    }
                ],
            }

    monkeypatch.setattr(download_backend.env_config, "YTDLP_YOUTUBE_PLAYER_CLIENTS", "default")
    monkeypatch.setattr(download_backend.yt_dlp, "YoutubeDL", FakeYDL)

    metadata = extract_metadata("https://www.youtube.com/watch?v=video")

    assert calls == 1
    assert metadata.original_audio_language == "ru"


def test_youtube_metadata_falls_back_when_original_track_is_not_exposed(monkeypatch) -> None:
    languages_seen: list[str | None] = []

    class FakeYDL:
        def __init__(self, options: dict) -> None:
            self.language = options.get("extractor_args", {}).get("youtube", {}).get("lang", [None])[0]

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def extract_info(self, *_args, **_kwargs):
            languages_seen.append(self.language)
            return {
                "id": "video",
                "extractor_key": "Youtube",
                "automatic_captions": {"ru-orig": [{"name": "Russian (Original)"}]},
                "formats": [
                    {
                        "format_id": "18",
                        "ext": "mp4",
                        "vcodec": "avc1",
                        "acodec": "mp4a",
                        "language": "en-US",
                        "language_preference": 5,
                    }
                ],
            }

    monkeypatch.setattr(download_backend.env_config, "YTDLP_YOUTUBE_PLAYER_CLIENTS", "default")
    monkeypatch.setattr(download_backend.yt_dlp, "YoutubeDL", FakeYDL)

    metadata = extract_metadata("https://www.youtube.com/watch?v=video")

    assert languages_seen == [None, "ru"]
    assert metadata.original_audio_language == "ru"
    assert metadata.info["formats"][0]["language"] == "en-US"


def test_youtube_metadata_skips_client_without_video_formats(monkeypatch) -> None:
    clients_seen: list[str] = []

    class FakeYDL:
        def __init__(self, options: dict) -> None:
            self.client = options.get("extractor_args", {}).get("youtube", {}).get("player_client", ["default"])[0]

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def extract_info(self, *_args, **_kwargs):
            clients_seen.append(self.client)
            if self.client == "default":
                return {"id": "video", "extractor_key": "Youtube", "formats": []}
            return {
                "id": "video",
                "extractor_key": "Youtube",
                "formats": [{"format_id": "18", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a"}],
            }

    monkeypatch.setattr(download_backend.env_config, "YTDLP_YOUTUBE_PLAYER_CLIENTS", "default,android,ios")
    monkeypatch.setattr(download_backend.yt_dlp, "YoutubeDL", FakeYDL)

    metadata = extract_metadata("https://www.youtube.com/watch?v=video")

    assert metadata.media_key == "youtube:video"
    assert clients_seen == ["default", "android"]


def test_youtube_audio_metadata_accepts_audio_only_client(monkeypatch) -> None:
    clients_seen: list[str] = []

    class FakeYDL:
        def __init__(self, options: dict) -> None:
            self.client = options.get("extractor_args", {}).get("youtube", {}).get("player_client", ["default"])[0]

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def extract_info(self, *_args, **_kwargs):
            clients_seen.append(self.client)
            return {
                "id": "audio",
                "duration": 120,
                "extractor_key": "Youtube",
                "formats": [{"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a"}],
            }

    monkeypatch.setattr(download_backend.env_config, "YTDLP_YOUTUBE_PLAYER_CLIENTS", "default,android")
    monkeypatch.setattr(download_backend.yt_dlp, "YoutubeDL", FakeYDL)

    metadata = extract_metadata("https://www.youtube.com/watch?v=audio", media_kind="audio")

    assert metadata.media_key == "youtube:audio"
    assert clients_seen == ["default"]


def test_generic_media_keys_include_url_identity(monkeypatch) -> None:
    class FakeYDL:
        def __init__(self, _options: dict) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def extract_info(self, url: str, **_kwargs):
            return {"id": "video", "extractor_key": "Generic", "webpage_url": url, "formats": []}

    monkeypatch.setattr(download_backend.yt_dlp, "YoutubeDL", FakeYDL)

    left = extract_metadata("https://one.example/video.mp4")
    right = extract_metadata("https://two.example/video.mp4")

    assert left.media_key != right.media_key
    assert left.media_key.startswith("generic:video:")


def test_youtube_client_fallback_chain_and_order(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(download_backend.env_config, "YTDLP_YOUTUBE_PLAYER_CLIENTS", "default,android,ios")
    clients_seen: list[str] = []
    formats_seen: list[tuple[str, str]] = []

    class FakeYDL:
        def __init__(self, options: dict) -> None:
            self.client = (
                options["extractor_args"]["youtube"]["player_client"][0]
                if "extractor_args" in options and "youtube" in options["extractor_args"]
                else "default"
            )
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def process_ie_result(self, *_args, **_kwargs):
            clients_seen.append(f"proc:{self.client}")
            formats_seen.append((self.client, self.options["format"]))
            if self.client == "ios":
                (tmp_path / "fallback.mp4").write_bytes(b"video")
                return {"id": "video"}
            raise RuntimeError("expired media URL")

        def extract_info(self, *_args, **_kwargs):
            clients_seen.append(f"extract:{self.client}")
            if self.client != "ios":
                raise RuntimeError(f"{self.client} blocked")
            return {
                "id": "video",
                "formats": [
                    {
                        "format_id": "ios-video",
                        "ext": "mp4",
                        "vcodec": "avc1",
                        "acodec": "mp4a",
                        "filesize": 4_000_000,
                    }
                ],
            }

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
                    "acodec": "mp4a",
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

    assert clients_seen == ["proc:default", "extract:default", "extract:android", "extract:ios", "proc:ios"]
    assert formats_seen == [("default", "video"), ("ios", "ios-video")]


def test_download_deadline_stops_before_another_attempt(monkeypatch, tmp_path: Path) -> None:
    class UnexpectedYDL:
        def __init__(self, _options: dict) -> None:
            raise AssertionError("yt-dlp must not start after the deadline")

    monkeypatch.setattr(download_backend.yt_dlp, "YoutubeDL", UnexpectedYDL)
    metadata = MediaMetadata(
        url="https://www.youtube.com/watch?v=video",
        info={"formats": []},
        media_key="youtube:video",
        source_name="YouTube",
    )

    with pytest.raises(DownloadDeadlineExceeded):
        download_metadata(
            metadata,
            "deadline",
            tmp_path,
            max_send_bytes=10_000_000,
            concurrent_fragments=2,
            deadline=0,
        )


def test_download_deadline_stops_fallback_after_progress_hook(monkeypatch, tmp_path: Path) -> None:
    expired = False
    process_calls = 0

    class FakeYDL:
        def __init__(self, options: dict) -> None:
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def process_ie_result(self, *_args, **_kwargs):
            nonlocal expired, process_calls
            process_calls += 1
            expired = True
            self.options["progress_hooks"][0]({})
            raise AssertionError("the progress hook must stop the attempt")

    monkeypatch.setattr(download_backend.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(download_backend.time, "monotonic", lambda: 2.0 if expired else 0.0)
    metadata = MediaMetadata(
        url="https://www.youtube.com/watch?v=video",
        info={
            "formats": [
                {
                    "format_id": "video",
                    "ext": "mp4",
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                    "filesize": 4_000_000,
                }
            ]
        },
        media_key="youtube:video",
        source_name="YouTube",
    )

    with pytest.raises(DownloadDeadlineExceeded):
        download_metadata(
            metadata,
            "deadline-progress",
            tmp_path,
            max_send_bytes=10_000_000,
            concurrent_fragments=2,
            deadline=1.0,
        )

    assert process_calls == 1


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


def test_youtube_download_reuses_discovered_original_language(monkeypatch, tmp_path: Path) -> None:
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
            (tmp_path / "original.mp4").write_bytes(b"video")
            return {"id": "video"}

    monkeypatch.setattr(download_backend.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(download_backend, "_validate_downloaded_video", lambda *_args, **_kwargs: None)
    metadata = MediaMetadata(
        url="https://www.youtube.com/watch?v=video",
        info={
            "id": "video",
            "formats": [
                {
                    "format_id": "18",
                    "ext": "mp4",
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                    "language": "ru",
                    "filesize": 4_000_000,
                }
            ],
        },
        media_key="youtube:video",
        source_name="YouTube",
        original_audio_language="ru",
    )

    result = download_metadata(
        metadata,
        "original",
        tmp_path,
        max_send_bytes=10_000_000,
        concurrent_fragments=2,
    )

    assert result["id"] == "video"
    assert options_seen[0]["extractor_args"] == {"youtube": {"lang": ["ru"]}}


def test_audio_download_uses_size_planned_mp3_postprocessor(monkeypatch, tmp_path: Path) -> None:
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
            (tmp_path / "audio.mp3").write_bytes(b"audio")
            return {"id": "audio"}

    monkeypatch.setattr(download_backend.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(download_backend, "_validate_downloaded_audio", lambda *_args, **_kwargs: None)
    metadata = MediaMetadata(
        url="https://example.com/media",
        info={
            "id": "audio",
            "duration": 60,
            "formats": [{"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a"}],
        },
        media_key="example:audio",
        source_name="Example",
    )

    result = download_metadata(
        metadata,
        "audio",
        tmp_path,
        max_send_bytes=10_000_000,
        concurrent_fragments=2,
        media_kind="audio",
    )

    assert result["id"] == "audio"
    assert len(options_seen) == 1
    assert options_seen[0]["format"] == "bestaudio/best"
    assert options_seen[0]["postprocessors"] == [
        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
    ]
