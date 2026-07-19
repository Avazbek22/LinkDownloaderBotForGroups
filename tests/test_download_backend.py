from __future__ import annotations

from pathlib import Path

from app import download_backend
from app.download_backend import MediaMetadata, download_metadata, select_format


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


def test_known_size_wins_over_higher_unknown_format() -> None:
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
    assert select_format(info, 50_000_000) == ("known", None)


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
            return {"id": "video", "extractor": "youtube"}

    monkeypatch.setattr(download_backend.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(
        download_backend,
        "_site_options",
        lambda *_args, **_kwargs: {"js_runtimes": {"node": {}}},
    )
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
