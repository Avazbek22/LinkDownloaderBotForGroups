from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import yt_dlp

import config
from app import env_config
from app.url_utils import is_instagram_url, is_youtube_url

try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
except Exception:  # pragma: no cover - old yt-dlp runtime
    ImpersonateTarget = None


def duration_sec(meta: Dict[str, Any]) -> Optional[int]:
    dur = meta.get("duration")
    if isinstance(dur, (int, float)) and dur > 0:
        return int(dur)
    return None


def format_size_bytes(fmt: Dict[str, Any], dur: Optional[int]) -> Tuple[Optional[int], bool]:
    fs = fmt.get("filesize")
    if isinstance(fs, int) and fs > 0:
        return fs, True

    fsa = fmt.get("filesize_approx")
    if isinstance(fsa, int) and fsa > 0:
        return fsa, True

    tbr = fmt.get("tbr")
    if dur and isinstance(tbr, (int, float)) and tbr > 0:
        est = int(dur * (float(tbr) * 1000.0 / 8.0))
        if est > 0:
            return est, False

    return None, False


def vcodec_pref_rank(vcodec: Any) -> int:
    s = str(vcodec or "")
    return 2 if s.startswith("avc1") else 1


def acodec_pref_rank(acodec: Any) -> int:
    s = str(acodec or "")
    return 2 if s.startswith("mp4a") else 1


def best_progressive_mp4(meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    best = None
    best_key = None

    for f in meta.get("formats", []) or []:
        if f.get("ext") != "mp4":
            continue
        if f.get("vcodec") == "none" or f.get("acodec") == "none":
            continue

        fid = f.get("format_id")
        if not fid:
            continue

        height = f.get("height") or 0
        fps = f.get("fps") or 0
        tbr = f.get("tbr") or 0

        v_rank = vcodec_pref_rank(f.get("vcodec"))
        a_rank = acodec_pref_rank(f.get("acodec"))

        key = (int(v_rank), int(a_rank), int(height), int(fps), float(tbr))

        if best is None or key > best_key:
            best = {
                "kind": "progressive",
                "format_spec": str(fid),
                "merge_output_format": None,
            }
            best_key = key

    return best


def best_separate_mp4_m4a(meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    dur = duration_sec(meta)

    best_v = None
    best_v_key = None
    best_a = None
    best_a_key = None

    for f in meta.get("formats", []) or []:
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        ext = f.get("ext")

        if ext == "mp4" and vcodec != "none" and acodec == "none":
            fid = f.get("format_id")
            if not fid:
                continue

            height = f.get("height") or 0
            fps = f.get("fps") or 0
            tbr = f.get("tbr") or 0

            v_rank = vcodec_pref_rank(vcodec)
            key = (int(v_rank), int(height), int(fps), float(tbr))

            if best_v is None or key > best_v_key:
                size, conf = format_size_bytes(f, dur)
                best_v = {"f": f, "size": size, "conf": conf}
                best_v_key = key

        if vcodec == "none" and acodec != "none" and ext in ("m4a", "mp4"):
            fid = f.get("format_id")
            if not fid:
                continue

            abr = f.get("abr") or f.get("tbr") or 0
            a_rank = acodec_pref_rank(acodec)
            key = (int(a_rank), float(abr))

            if best_a is None or key > best_a_key:
                size, conf = format_size_bytes(f, dur)
                best_a = {"f": f, "size": size, "conf": conf}
                best_a_key = key

    if not best_v or not best_a:
        return None

    vf = best_v["f"]
    af = best_a["f"]
    return {
        "kind": "separate",
        "format_spec": f"{vf.get('format_id')}+{af.get('format_id')}",
        "merge_output_format": "mp4",
    }


def build_video_plan_like_main1(meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    p = best_progressive_mp4(meta)
    if p:
        return p
    p = best_separate_mp4_m4a(meta)
    if p:
        return p
    return None


def _parse_csv_list(raw: str) -> List[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def _build_http_headers() -> Dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    }


def _parse_impersonate_target(raw: str) -> Optional[Any]:
    name = (raw or "").strip()
    if not name or ImpersonateTarget is None:
        return None
    try:
        return ImpersonateTarget.from_str(name)
    except Exception:
        return None


def _youtube_api_opts() -> Dict[str, Any]:
    runtimes = _parse_csv_list(env_config.YTDLP_JS_RUNTIMES)
    remote_components = _parse_csv_list(env_config.YTDLP_REMOTE_COMPONENTS)
    opts: Dict[str, Any] = {}
    if runtimes:
        opts["js_runtimes"] = {name: {} for name in runtimes}
    if remote_components:
        opts["remote_components"] = remote_components
    return opts


def _instagram_api_opts(force_impersonate: bool) -> Dict[str, Any]:
    opts: Dict[str, Any] = {}
    if force_impersonate:
        target = _parse_impersonate_target(env_config.YTDLP_INSTAGRAM_IMPERSONATE)
        if target is not None:
            opts["impersonate"] = target
    return opts


def _base_meta_opts(cookiefile_value: Optional[str], http_headers: Dict[str, str]) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 5,
        "http_headers": http_headers,
    }
    if cookiefile_value:
        opts["cookiefile"] = cookiefile_value
    return opts


def _base_download_opts(
    outtmpl: str,
    max_send_bytes: int,
    concurrent_fragments: int,
    http_headers: Dict[str, str],
) -> Dict[str, Any]:
    return {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "concurrent_fragment_downloads": concurrent_fragments,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 20,
        "max_filesize": max_send_bytes,
        "http_headers": http_headers,
    }


def _extract_meta(url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _download(url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


def download_with_ytdlp(
    url: str,
    out_prefix: str,
    output_folder: str,
    *,
    max_send_bytes: int,
    concurrent_fragments: int,
) -> Dict[str, Any]:
    os.makedirs(output_folder, exist_ok=True)
    outtmpl = os.path.join(output_folder, f"{out_prefix}.%(ext)s")

    cookies_file = getattr(config, "cookies_file", None)
    cookiefile_value = None
    if isinstance(cookies_file, str) and cookies_file.strip() and os.path.exists(cookies_file.strip()):
        cookiefile_value = cookies_file.strip()

    http_headers = _build_http_headers()

    is_yt = is_youtube_url(url)
    is_ig = is_instagram_url(url)

    meta_opts = _base_meta_opts(cookiefile_value, http_headers)
    if is_yt:
        meta_opts.update(_youtube_api_opts())
    if is_ig:
        meta_opts.update(
            {
                "retries": env_config.YTDLP_INSTAGRAM_RETRIES,
                "fragment_retries": env_config.YTDLP_INSTAGRAM_FRAGMENT_RETRIES,
                "socket_timeout": env_config.YTDLP_INSTAGRAM_SOCKET_TIMEOUT,
            }
        )
        meta_opts.update(_instagram_api_opts(force_impersonate=True))

    try:
        meta = _extract_meta(url, meta_opts)
    except Exception:
        if not is_ig:
            raise
        # Fail-soft: Instagram fallback without forced impersonation and with base retries/timeouts.
        fallback_meta_opts = _base_meta_opts(cookiefile_value, http_headers)
        meta = _extract_meta(url, fallback_meta_opts)

    plan = build_video_plan_like_main1(meta)
    if plan:
        format_value = str(plan.get("format_spec"))
        merge_value = plan.get("merge_output_format")
    else:
        format_value = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
        merge_value = "mp4"

    primary_opts = _base_download_opts(
        outtmpl=outtmpl,
        max_send_bytes=max_send_bytes,
        concurrent_fragments=concurrent_fragments,
        http_headers=http_headers,
    )
    primary_opts["format"] = format_value
    if merge_value:
        primary_opts["merge_output_format"] = str(merge_value)
    if cookiefile_value:
        primary_opts["cookiefile"] = cookiefile_value
    if is_yt:
        primary_opts.update(_youtube_api_opts())
    if is_ig:
        primary_opts.update(
            {
                "retries": env_config.YTDLP_INSTAGRAM_RETRIES,
                "fragment_retries": env_config.YTDLP_INSTAGRAM_FRAGMENT_RETRIES,
                "socket_timeout": env_config.YTDLP_INSTAGRAM_SOCKET_TIMEOUT,
            }
        )
        primary_opts.update(_instagram_api_opts(force_impersonate=True))

    try:
        return _download(url, primary_opts)
    except Exception as primary_err:
        if is_ig:
            # Fail-soft: retry Instagram without forced impersonation and with default retries/timeouts.
            ig_fallback_opts = _base_download_opts(
                outtmpl=outtmpl,
                max_send_bytes=max_send_bytes,
                concurrent_fragments=concurrent_fragments,
                http_headers=http_headers,
            )
            ig_fallback_opts["format"] = format_value
            if merge_value:
                ig_fallback_opts["merge_output_format"] = str(merge_value)
            if cookiefile_value:
                ig_fallback_opts["cookiefile"] = cookiefile_value
            return _download(url, ig_fallback_opts)

        if is_yt:
            # Extractor churn fallback for YouTube.
            yt_fallback_opts = _base_download_opts(
                outtmpl=outtmpl,
                max_send_bytes=max_send_bytes,
                concurrent_fragments=1,
                http_headers=http_headers,
            )
            yt_fallback_opts.update(_youtube_api_opts())
            yt_fallback_opts["format"] = "18/best[ext=mp4]/best"
            return _download(url, yt_fallback_opts)

        raise primary_err
