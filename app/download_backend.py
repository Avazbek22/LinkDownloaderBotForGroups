from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp

from app import env_config
from app.url_utils import is_instagram_url, is_youtube_url

try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
except ImportError:  # pragma: no cover - compatibility with older yt-dlp
    ImpersonateTarget = None


@dataclass(frozen=True)
class MediaMetadata:
    url: str
    info: dict[str, Any]
    media_key: str
    source_name: str


_SOURCE_NAMES = {
    "youtube": "YouTube",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "twitter": "X",
    "facebook": "Facebook",
    "vkontakte": "VK",
    "vk": "VK",
}


def display_source_name(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "Video"
    key = re.sub(r"[^a-z0-9]", "", raw.lower())
    return _SOURCE_NAMES.get(key, raw)


def _duration(info: dict[str, Any]) -> int | None:
    value = info.get("duration")
    return int(value) if isinstance(value, (int, float)) and value > 0 else None


def _size(fmt: dict[str, Any], duration: int | None) -> int | None:
    for key in ("filesize", "filesize_approx"):
        value = fmt.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    bitrate = fmt.get("tbr")
    if duration and isinstance(bitrate, (int, float)) and bitrate > 0:
        return int(duration * bitrate * 1000 / 8)
    return None


def _video_score(fmt: dict[str, Any]) -> tuple[int, int, int, float]:
    height = min(int(fmt.get("height") or 0), 2160)
    compatible = int(str(fmt.get("vcodec") or "").startswith("avc1"))
    fps = min(int(fmt.get("fps") or 0), 120)
    bitrate = float(fmt.get("tbr") or 0)
    return height, compatible, fps, bitrate


def _audio_score(fmt: dict[str, Any]) -> tuple[int, float]:
    compatible = int(str(fmt.get("acodec") or "").startswith("mp4a"))
    return compatible, float(fmt.get("abr") or fmt.get("tbr") or 0)


def select_format(info: dict[str, Any], max_bytes: int) -> tuple[str, str | None]:
    """Select the best MP4 plan that is likely to fit the Telegram limit."""
    formats = [item for item in info.get("formats", []) if isinstance(item, dict)]
    duration = _duration(info)
    budget = int(max_bytes * 0.96)

    progressive: list[dict[str, Any]] = []
    video_only: list[dict[str, Any]] = []
    audio_only: list[dict[str, Any]] = []
    for fmt in formats:
        if not fmt.get("format_id"):
            continue
        video = fmt.get("vcodec") not in {None, "none"}
        audio = fmt.get("acodec") not in {None, "none"}
        ext = fmt.get("ext")
        if ext == "mp4" and video and audio:
            if (_size(fmt, duration) or budget) <= budget:
                progressive.append(fmt)
        elif ext == "mp4" and video and not audio:
            video_only.append(fmt)
        elif ext in {"m4a", "mp4"} and audio and not video:
            audio_only.append(fmt)

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for video in video_only:
        video_size = _size(video, duration)
        for audio in audio_only:
            audio_size = _size(audio, duration)
            if video_size is not None and audio_size is not None and video_size + audio_size > budget:
                continue
            pairs.append((video, audio))
    known_progressive = [fmt for fmt in progressive if _size(fmt, duration) is not None]
    if known_progressive:
        progressive = known_progressive
    known_pairs = [
        pair for pair in pairs if _size(pair[0], duration) is not None and _size(pair[1], duration) is not None
    ]
    if known_pairs:
        pairs = known_pairs

    best_progressive = max(progressive, key=_video_score) if progressive else None
    best_pair = max(pairs, key=lambda pair: (_video_score(pair[0]), _audio_score(pair[1]))) if pairs else None
    if best_progressive is not None and (
        best_pair is None or _video_score(best_progressive) >= _video_score(best_pair[0])
    ):
        return str(best_progressive["format_id"]), None
    if best_pair is not None:
        video, audio = best_pair
        return f"{video['format_id']}+{audio['format_id']}", "mp4"

    # yt-dlp applies the actual max_filesize guard. This fallback is needed when
    # extractors do not expose enough size metadata to make a local decision.
    return "b[ext=mp4]/bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b", "mp4"


def _csv(raw: str) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _headers() -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    }


def _impersonate(raw: str) -> Any | None:
    if not raw or ImpersonateTarget is None:
        return None
    try:
        return ImpersonateTarget.from_str(raw.strip())
    except (ValueError, TypeError):
        return None


def _site_options(url: str, *, instagram_impersonate: bool = True) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if is_youtube_url(url):
        runtimes = _csv(env_config.YTDLP_JS_RUNTIMES)
        components = _csv(env_config.YTDLP_REMOTE_COMPONENTS)
        if runtimes:
            options["js_runtimes"] = {runtime: {} for runtime in runtimes}
        if components:
            options["remote_components"] = components
    if is_instagram_url(url):
        options.update(
            retries=env_config.YTDLP_INSTAGRAM_RETRIES,
            fragment_retries=env_config.YTDLP_INSTAGRAM_FRAGMENT_RETRIES,
            socket_timeout=env_config.YTDLP_INSTAGRAM_SOCKET_TIMEOUT,
        )
        target = _impersonate(env_config.YTDLP_INSTAGRAM_IMPERSONATE) if instagram_impersonate else None
        if target is not None:
            options["impersonate"] = target
    return options


def _base_options(cookie_file: Path | None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 5,
        "http_headers": _headers(),
    }
    if cookie_file and cookie_file.is_file():
        options["cookiefile"] = os.fspath(cookie_file)
    return options


def extract_metadata(url: str, cookie_file: Path | None = None) -> MediaMetadata:
    options = _base_options(cookie_file)
    options.update(_site_options(url))
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        if not is_instagram_url(url):
            raise
        fallback = _base_options(cookie_file)
        with yt_dlp.YoutubeDL(fallback) as ydl:
            info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise RuntimeError("extractor returned no metadata")
    extractor = str(info.get("extractor_key") or info.get("extractor") or "generic").lower()
    media_id = str(info.get("id") or info.get("display_id") or "").strip()
    if not media_id:
        raise RuntimeError("extractor returned no media id")
    source = display_source_name(str(info.get("extractor_key") or info.get("extractor") or "Video"))
    return MediaMetadata(url=url, info=info, media_key=f"{extractor}:{media_id}", source_name=source)


def download_metadata(
    metadata: MediaMetadata,
    out_prefix: str,
    output_folder: Path,
    *,
    max_send_bytes: int,
    concurrent_fragments: int,
    cookie_file: Path | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    output_folder.mkdir(parents=True, exist_ok=True)
    outtmpl = os.fspath(output_folder / f"{out_prefix}.%(ext)s")
    format_spec, merge_format = select_format(metadata.info, max_send_bytes)

    def progress_hook(_status: dict[str, Any]) -> None:
        if deadline is not None and time.monotonic() > deadline:
            raise yt_dlp.utils.DownloadError("download deadline exceeded")

    options = _base_options(cookie_file)
    options.update(
        outtmpl=outtmpl,
        concurrent_fragment_downloads=concurrent_fragments,
        fragment_retries=5,
        max_filesize=max_send_bytes,
        format=format_spec,
        progress_hooks=[progress_hook],
    )
    if merge_format:
        options["merge_output_format"] = merge_format
    options.update(_site_options(metadata.url))
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.process_ie_result(dict(metadata.info), download=True)
    except Exception:
        if not (is_youtube_url(metadata.url) or is_instagram_url(metadata.url)):
            raise
        fallback = _base_options(cookie_file)
        fallback.update(
            outtmpl=outtmpl,
            concurrent_fragment_downloads=1,
            fragment_retries=5,
            max_filesize=max_send_bytes,
            format=format_spec,
            progress_hooks=[progress_hook],
        )
        if merge_format:
            fallback["merge_output_format"] = merge_format
        fallback.update(_site_options(metadata.url, instagram_impersonate=False))
        with yt_dlp.YoutubeDL(fallback) as ydl:
            return ydl.extract_info(metadata.url, download=True)


def find_downloaded_file(info: dict[str, Any], prefix: str, output_folder: Path) -> Path | None:
    exact = output_folder / f"{prefix}.mp4"
    if exact.is_file():
        return exact
    candidates: list[Path] = []
    for path in output_folder.glob(f"{prefix}.*"):
        lower = path.name.lower()
        if not path.is_file() or lower.endswith((".part", ".ytdl", ".tmp", ".temp")):
            continue
        name_after_prefix = lower[len(prefix) :]
        if name_after_prefix.startswith(".f") and name_after_prefix[2:].split(".", 1)[0].isdigit():
            continue
        candidates.append(path)
    if not candidates:
        return None
    candidates.sort(key=lambda path: (path.suffix.lower() == ".mp4", path.stat().st_mtime), reverse=True)
    return candidates[0]
