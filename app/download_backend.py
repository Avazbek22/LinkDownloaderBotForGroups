from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp

from app import env_config
from app.url_security import safe_error_for_log
from app.url_utils import is_instagram_url, is_youtube_url

LOG = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class FormatPlan:
    format_spec: str
    merge_output_format: str | None = None


class InstagramContentRestrictedError(RuntimeError):
    """Instagram did not expose the requested content to this client."""


class DownloadDeadlineExceeded(RuntimeError):
    """The configured end-to-end download deadline has elapsed."""


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


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _video_compatibility(fmt: dict[str, Any]) -> int:
    codec = str(fmt.get("vcodec") or "").lower()
    if codec.startswith(("avc1", "avc3", "h264")):
        return 2
    if not codec:
        # Some extractors expose a direct MP4 without probing its streams. Such
        # files are usually the site's broadly compatible progressive variant.
        return 1
    return 0


def _video_score(fmt: dict[str, Any]) -> tuple[int, int, int, float]:
    compatible = _video_compatibility(fmt)
    height = min(_safe_int(fmt.get("height")), 2160)
    fps = min(_safe_int(fmt.get("fps")), 120)
    bitrate = _safe_float(fmt.get("tbr"))
    return compatible, height, fps, bitrate


def _audio_score(fmt: dict[str, Any]) -> tuple[int, float]:
    compatible = int(str(fmt.get("acodec") or "").startswith("mp4a"))
    return compatible, _safe_float(fmt.get("abr") or fmt.get("tbr"))


def select_format_candidates(info: dict[str, Any], max_bytes: int) -> list[FormatPlan]:
    """Return best-first MP4 plans that may fit and remain Telegram-compatible."""
    raw_formats = info.get("formats")
    formats = [item for item in raw_formats if isinstance(item, dict)] if isinstance(raw_formats, list) else []
    duration = _duration(info)
    budget = int(max_bytes * 0.96)

    progressive: list[dict[str, Any]] = []
    video_only: list[dict[str, Any]] = []
    audio_only: list[dict[str, Any]] = []
    for fmt in formats:
        if not fmt.get("format_id"):
            continue
        video_codec = fmt.get("vcodec")
        audio_codec = fmt.get("acodec")
        video = video_codec not in {None, "none"}
        audio = audio_codec not in {None, "none"}
        ext = fmt.get("ext")
        unknown_direct_mp4 = ext == "mp4" and video_codec is None and audio_codec is None and bool(fmt.get("url"))
        if ext == "mp4" and ((video and audio) or unknown_direct_mp4) and _video_compatibility(fmt) > 0:
            if (_size(fmt, duration) or budget) <= budget:
                progressive.append(fmt)
        elif ext == "mp4" and video and not audio and _video_compatibility(fmt) > 0:
            video_only.append(fmt)
        elif ext in {"m4a", "mp4"} and audio and not video:
            audio_only.append(fmt)

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for video in video_only:
        video_size = _size(video, duration)
        if video_size is not None and video_size > budget:
            continue
        for audio in audio_only:
            audio_size = _size(audio, duration)
            if audio_size is not None and audio_size > budget:
                continue
            if video_size is not None and audio_size is not None and video_size + audio_size > budget:
                continue
            pairs.append((video, audio))
    ranked: list[tuple[tuple[Any, ...], FormatPlan, str | None]] = []
    for fmt in progressive:
        video_score = _video_score(fmt)
        score = (*video_score[:3], int(_size(fmt, duration) is not None), video_score[3])
        ranked.append((score, FormatPlan(str(fmt["format_id"])), str(fmt.get("url") or "") or None))
    for video, audio in pairs:
        video_score = _video_score(video)
        known_size = int(_size(video, duration) is not None and _size(audio, duration) is not None)
        ranked.append(
            (
                (*video_score[:3], known_size, video_score[3], *_audio_score(audio)),
                FormatPlan(f"{video['format_id']}+{audio['format_id']}", "mp4"),
                None,
            )
        )
    ranked.sort(key=lambda item: item[0], reverse=True)

    plans: list[FormatPlan] = []
    seen_specs: set[str] = set()
    seen_direct_urls: set[str] = set()
    for _score, plan, direct_url in ranked:
        # Instagram sometimes lists the same direct MP4 under several opaque
        # IDs. Downloading the identical URL repeatedly cannot improve quality.
        if direct_url and direct_url in seen_direct_urls:
            continue
        if plan.format_spec in seen_specs:
            continue
        plans.append(plan)
        seen_specs.add(plan.format_spec)
        if direct_url:
            seen_direct_urls.add(direct_url)

    if not plans:
        plans.append(
            FormatPlan(
                "b[ext=mp4][vcodec^=avc1][acodec!=none]/"
                "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/"
                "b[ext=mp4][vcodec^=h264][acodec!=none]/"
                "bv*[ext=mp4][vcodec^=h264]+ba[ext=m4a]",
                "mp4",
            )
        )
    return plans


def select_format(info: dict[str, Any], max_bytes: int) -> tuple[str, str | None]:
    """Return the preferred plan while preserving the original public API."""
    plans = select_format_candidates(info, max_bytes)
    if not plans:
        raise RuntimeError("no compatible video format fits the configured size limit")
    plan = plans[0]
    return plan.format_spec, plan.merge_output_format


def has_downloadable_video(info: dict[str, Any]) -> bool:
    """Return whether extractor metadata positively identifies a video stream."""
    raw_formats = info.get("formats")
    formats = [item for item in raw_formats if isinstance(item, dict)] if isinstance(raw_formats, list) else []
    candidates = [info, *formats]
    for candidate in candidates:
        video_codec = candidate.get("vcodec")
        if video_codec not in {None, "none"}:
            return True
        if (
            candidate.get("ext") == "mp4"
            and video_codec is None
            and candidate.get("acodec") is None
            and bool(candidate.get("url"))
        ):
            # Some direct progressive MP4 entries (notably Instagram) omit
            # codec metadata. The final ffprobe validation remains authoritative.
            return True
    raw_entries = info.get("entries")
    entries = [entry for entry in raw_entries if isinstance(entry, dict)] if isinstance(raw_entries, list) else []
    return any(has_downloadable_video(entry) for entry in entries)


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


def _site_options(
    url: str,
    *,
    instagram_impersonate: bool = True,
    youtube_player_client: str | None = None,
) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if is_youtube_url(url):
        runtimes = _csv(env_config.YTDLP_JS_RUNTIMES)
        components = _csv(env_config.YTDLP_REMOTE_COMPONENTS)
        if runtimes:
            options["js_runtimes"] = {runtime: {} for runtime in runtimes}
        if components:
            options["remote_components"] = components
        if youtube_player_client is not None:
            options["extractor_args"] = {
                "youtube": {"player_client": [youtube_player_client]},
            }
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


def _youtube_player_clients() -> list[str | None]:
    clients_raw = env_config.YTDLP_YOUTUBE_PLAYER_CLIENTS
    if not clients_raw.strip() and env_config.YTDLP_YOUTUBE_PLAYER_CLIENT.strip():
        clients_raw = env_config.YTDLP_YOUTUBE_PLAYER_CLIENT

    clients = _csv(clients_raw)
    if not clients:
        clients = ["default", "android", "ios"]

    ordered: list[str | None] = []
    for client in clients:
        normalized = client.strip().lower()
        if not normalized:
            continue
        # "web" was historically treated as yt-dlp's own default selection.
        # Keep that alias so existing .env files do not silently change behavior.
        value = None if normalized in {"default", "web"} else normalized
        if value in ordered:
            continue
        ordered.append(value)

    return ordered or [None, "android", "ios"]


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise DownloadDeadlineExceeded("download deadline exceeded")


def _is_instagram_content_restriction(error: BaseException) -> bool:
    """Recognize Instagram responses that explicitly hide requested content."""
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).casefold().replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")
        if "[instagram]" in message and (
            "available to everyone" in message
            or "seen by certain audiences" in message
            or "instagram sent an empty media response" in message
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


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


def _unselect_info(info: dict[str, Any]) -> dict[str, Any]:
    """Remove yt-dlp's default choice so a later FormatPlan is actually applied."""
    clean = dict(info)
    for key in (
        "requested_formats",
        "requested_downloads",
        "format_id",
        "format",
        "url",
        "manifest_url",
        "ext",
        "vcodec",
        "acodec",
        "width",
        "height",
        "resolution",
        "filesize",
        "filesize_approx",
        "tbr",
        "vbr",
        "abr",
        "protocol",
        "container",
    ):
        clean.pop(key, None)
    return clean


def _remove_attempt_files(output_folder: Path, out_prefix: str) -> None:
    for path in output_folder.glob(f"{out_prefix}.*"):
        if path.is_file():
            path.unlink(missing_ok=True)


def _validate_downloaded_video(path: Path, max_bytes: int) -> None:
    """Reject partial, audio-only, oversized, or Telegram-incompatible results."""
    size = path.stat().st_size
    if size <= 0:
        raise RuntimeError("downloaded file is empty")
    if size > max_bytes:
        raise RuntimeError("downloaded file exceeds MAX_FILESIZE")

    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=format_name:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            os.fspath(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(completed.stdout)
    streams = payload.get("streams") if isinstance(payload, dict) else None
    video_streams = [
        stream for stream in (streams or []) if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    if not video_streams:
        raise RuntimeError("downloaded file has no video stream")

    compatible = any(
        str(stream.get("codec_name") or "").lower() == "h264"
        and int(stream.get("width") or 0) > 0
        and int(stream.get("height") or 0) > 0
        for stream in video_streams
    )
    if not compatible:
        codecs = ",".join(sorted({str(stream.get("codec_name") or "unknown") for stream in video_streams}))
        raise RuntimeError(f"downloaded video codec is not Telegram-compatible: {codecs}")

    format_info = payload.get("format") if isinstance(payload, dict) else None
    format_name = str(format_info.get("format_name") or "") if isinstance(format_info, dict) else ""
    if not {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}.intersection(format_name.split(",")):
        raise RuntimeError(f"downloaded video container is not MP4-compatible: {format_name or 'unknown'}")


def extract_metadata(
    url: str,
    cookie_file: Path | None = None,
    deadline: float | None = None,
) -> MediaMetadata:
    _check_deadline(deadline)
    info: Any
    if is_youtube_url(url):
        last_error: Exception | None = None
        info = None
        for player_client in _youtube_player_clients():
            _check_deadline(deadline)
            options = _base_options(cookie_file)
            options.update(_site_options(url, youtube_player_client=player_client))
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    candidate = ydl.extract_info(url, download=False)
                if not isinstance(candidate, dict):
                    raise RuntimeError("extractor returned no metadata")
                info = candidate
                break
            except Exception as error:
                last_error = error
                LOG.info(
                    "YouTube metadata client failed client=%s error=%s detail=%s",
                    player_client or "default",
                    type(error).__name__,
                    safe_error_for_log(error),
                )
                _check_deadline(deadline)
        if info is None:
            raise RuntimeError("all YouTube metadata clients failed") from last_error
    else:
        options = _base_options(cookie_file)
        options.update(_site_options(url))
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as primary_error:
            _check_deadline(deadline)
            if not is_instagram_url(url):
                raise
            fallback = _base_options(cookie_file)
            try:
                with yt_dlp.YoutubeDL(fallback) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as fallback_error:
                _check_deadline(deadline)
                # Preserve an explicit content restriction even if Instagram gives
                # the fallback request a less useful generic error.
                restricted_error = next(
                    (error for error in (primary_error, fallback_error) if _is_instagram_content_restriction(error)),
                    None,
                )
                if restricted_error is not None:
                    raise InstagramContentRestrictedError(
                        "Instagram did not expose this content to the bot"
                    ) from restricted_error
                raise
    _check_deadline(deadline)
    if not isinstance(info, dict):
        raise RuntimeError("extractor returned no metadata")
    extractor = str(info.get("extractor_key") or info.get("extractor") or "generic").lower()
    media_id = str(info.get("id") or info.get("display_id") or "").strip()
    if not media_id:
        raise RuntimeError("extractor returned no media id")
    source = display_source_name(str(info.get("extractor_key") or info.get("extractor") or "Video"))
    media_key = f"{extractor}:{media_id}"
    if extractor == "generic":
        identity_url = str(info.get("webpage_url") or url)
        identity_hash = hashlib.sha256(identity_url.encode("utf-8")).hexdigest()[:24]
        media_key = f"generic:{media_id}:{identity_hash}"
    return MediaMetadata(url=url, info=info, media_key=media_key, source_name=source)


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
    is_youtube = is_youtube_url(metadata.url)
    is_instagram = is_instagram_url(metadata.url)

    def progress_hook(_status: dict[str, Any]) -> None:
        try:
            _check_deadline(deadline)
        except DownloadDeadlineExceeded:
            raise yt_dlp.utils.DownloadError("download deadline exceeded") from None

    def extraction_options(
        *,
        instagram_impersonate: bool = True,
        youtube_player_client: str | None = None,
    ) -> dict[str, Any]:
        options = _base_options(cookie_file)
        options.update(
            _site_options(
                metadata.url,
                instagram_impersonate=instagram_impersonate,
                youtube_player_client=youtube_player_client,
            )
        )
        return options

    def options_for(
        plan: FormatPlan,
        fragments: int,
        *,
        instagram_impersonate: bool = True,
        youtube_player_client: str | None = None,
    ) -> dict[str, Any]:
        options = _base_options(cookie_file)
        options.update(
            outtmpl=outtmpl,
            concurrent_fragment_downloads=fragments,
            fragment_retries=5,
            max_filesize=max_send_bytes,
            format=plan.format_spec,
            progress_hooks=[progress_hook],
        )
        if plan.merge_output_format:
            options["merge_output_format"] = plan.merge_output_format
        options.update(
            _site_options(
                metadata.url,
                instagram_impersonate=instagram_impersonate,
                youtube_player_client=youtube_player_client,
            )
        )
        return options

    last_error: Exception | None = None

    def try_info(
        info: dict[str, Any],
        *,
        phase: str,
        fragments: int,
        instagram_impersonate: bool = True,
        youtube_player_client: str | None = None,
    ) -> dict[str, Any] | None:
        nonlocal last_error
        plans = select_format_candidates(info, max_send_bytes)
        for index, plan in enumerate(plans, start=1):
            _check_deadline(deadline)
            _remove_attempt_files(output_folder, out_prefix)
            LOG.info(
                "trying media format phase=%s client=%s candidate=%s total=%s",
                phase,
                youtube_player_client or "default",
                index,
                len(plans),
            )
            try:
                with yt_dlp.YoutubeDL(
                    options_for(
                        plan,
                        fragments,
                        youtube_player_client=youtube_player_client,
                        instagram_impersonate=instagram_impersonate,
                    )
                ) as ydl:
                    result = ydl.process_ie_result(_unselect_info(info), download=True)
                if not isinstance(result, dict):
                    raise RuntimeError("extractor returned no download result")
                path = find_downloaded_file(result, out_prefix, output_folder)
                if path is None:
                    raise RuntimeError("downloaded file not found")
                _validate_downloaded_video(path, max_send_bytes)
                _check_deadline(deadline)
                return result
            except Exception as error:
                last_error = error
                _remove_attempt_files(output_folder, out_prefix)
                LOG.info(
                    "media format failed phase=%s client=%s candidate=%s error=%s detail=%s",
                    phase,
                    youtube_player_client or "default",
                    index,
                    type(error).__name__,
                    safe_error_for_log(error),
                )
                _check_deadline(deadline)
        return None

    _check_deadline(deadline)
    result = try_info(
        metadata.info,
        phase="initial",
        fragments=concurrent_fragments,
    )
    if result is not None:
        return result

    if is_youtube:
        # The player client only affects extraction. Reusing metadata URLs with
        # different extractor_args merely retries the same CDN URLs, so each
        # fallback client gets one fresh extraction and its own format plans.
        for player_client in _youtube_player_clients():
            _check_deadline(deadline)
            _remove_attempt_files(output_folder, out_prefix)
            try:
                with yt_dlp.YoutubeDL(extraction_options(youtube_player_client=player_client)) as ydl:
                    fresh_info = ydl.extract_info(metadata.url, download=False)
                if not isinstance(fresh_info, dict):
                    raise RuntimeError("extractor returned no metadata")
            except Exception as error:
                last_error = error
                LOG.info(
                    "YouTube fallback extraction failed client=%s error=%s detail=%s",
                    player_client or "default",
                    type(error).__name__,
                    safe_error_for_log(error),
                )
                _check_deadline(deadline)
                continue
            result = try_info(
                fresh_info,
                phase="fresh",
                fragments=1,
                youtube_player_client=player_client,
            )
            if result is not None:
                return result
    elif is_instagram:
        # Instagram is sensitive to duplicate requests, so retain only one
        # fallback extraction without browser impersonation.
        _check_deadline(deadline)
        _remove_attempt_files(output_folder, out_prefix)
        try:
            with yt_dlp.YoutubeDL(extraction_options(instagram_impersonate=False)) as ydl:
                fresh_info = ydl.extract_info(metadata.url, download=False)
            if not isinstance(fresh_info, dict):
                raise RuntimeError("extractor returned no metadata")
            result = try_info(
                fresh_info,
                phase="fresh",
                fragments=1,
                instagram_impersonate=False,
            )
            if result is not None:
                return result
        except Exception as error:
            last_error = error
            _check_deadline(deadline)

    _remove_attempt_files(output_folder, out_prefix)
    _check_deadline(deadline)
    raise RuntimeError("no compatible video format fits the configured size limit") from last_error


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
