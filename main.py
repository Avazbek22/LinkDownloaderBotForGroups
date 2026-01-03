from __future__ import annotations

import html
import json
import os
import re
import uuid
import queue
import threading
from dataclasses import dataclass
from typing import Optional, Dict, Any
from urllib.parse import urlparse

import telebot
import yt_dlp

import config


# =========================
# Settings (simple and stable)
# =========================

WORKERS = 2                 # how many downloads in parallel
MAX_QUEUE = 200             # queue limit to avoid RAM issues on busy groups
MAX_SEND_BYTES = int(getattr(config, "max_filesize", 50_000_000))
YTDLP_CONCURRENT_FRAGMENTS = 4

# Persistent JSON "DB" (mounted via docker-compose to survive restarts)
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
PREFS_PATH = os.path.join(DATA_DIR, "prefs.json")

# About (for help messages)
AUTHOR_NAME = "Avazbek Olimov"
REPO_URL = "https://github.com/Avazbek22/LinkDownloaderBotForGroups"


# =========================
# Telegram init
# =========================

bot = telebot.TeleBot(config.token, threaded=True)
bot_lock = threading.RLock()

jobs_q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=MAX_QUEUE)


def _bot_call(fn, *args, **kwargs):
    with bot_lock:
        return fn(*args, **kwargs)


# =========================
# Helpers
# =========================

def _extract_first_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"(https?://\S+)", text.strip())
    if not m:
        return None
    url = m.group(1).strip()
    url = url.rstrip(").,]}>\"'")
    return url


def _safe_send_message(chat_id: int, text: str, message_thread_id: Optional[int] = None) -> None:
    # Requirement: no notifications, no preview
    try:
        kwargs: Dict[str, Any] = {
            "disable_web_page_preview": True,
            "disable_notification": True,
        }
        if isinstance(message_thread_id, int):
            kwargs["message_thread_id"] = message_thread_id

        _bot_call(bot.send_message, chat_id, text, **kwargs)
    except Exception:
        pass


def _safe_send_message_html(chat_id: int, html_text: str, message_thread_id: Optional[int] = None) -> None:
    # Requirement: no notifications, no preview + HTML allowed
    try:
        kwargs: Dict[str, Any] = {
            "disable_web_page_preview": True,
            "disable_notification": True,
            "parse_mode": "HTML",
        }
        if isinstance(message_thread_id, int):
            kwargs["message_thread_id"] = message_thread_id

        _bot_call(bot.send_message, chat_id, html_text, **kwargs)
    except Exception:
        pass


def _safe_delete_message(chat_id: int, message_id: int) -> bool:
    try:
        _bot_call(bot.delete_message, chat_id, message_id)
        return True
    except Exception:
        return False


def _log_request(message, url: str) -> None:
    logs_chat = getattr(config, "logs", None)
    if not logs_chat:
        return

    try:
        user = message.from_user
        username = f"@{user.username}" if getattr(user, "username", None) else "(no username)"
        chat_title = getattr(message.chat, "title", "") or "Group"
        text = (
            f"Download request from {username} ({user.id})\n"
            f"Chat: {chat_title} ({message.chat.id})\n"
            f"URL: {url}"
        )
        _bot_call(bot.send_message, logs_chat, text, disable_web_page_preview=True)
    except Exception:
        pass


def _find_downloaded_file(info: Dict[str, Any], prefix: str, output_folder: str) -> Optional[str]:
    # 1) preferred: requested_downloads[*].filepath
    try:
        reqs = info.get("requested_downloads") or []
        for r in reqs:
            fp = r.get("filepath")
            if fp and os.path.exists(fp):
                return fp
    except Exception:
        pass

    # 2) fallback: search by prefix in output folder
    try:
        for fn in os.listdir(output_folder):
            if fn.startswith(prefix):
                fp = os.path.join(output_folder, fn)
                if os.path.exists(fp):
                    return fp
    except Exception:
        pass

    return None


def _cleanup_files(prefix: str, output_folder: str) -> None:
    try:
        for fn in os.listdir(output_folder):
            if fn.startswith(prefix):
                fp = os.path.join(output_folder, fn)
                try:
                    if os.path.exists(fp):
                        os.remove(fp)
                except Exception:
                    pass
    except Exception:
        pass


def _download_with_ytdlp(url: str, out_prefix: str, output_folder: str) -> Dict[str, Any]:
    os.makedirs(output_folder, exist_ok=True)

    outtmpl = os.path.join(output_folder, f"{out_prefix}.%(ext)s")

    ydl_opts: Dict[str, Any] = {
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "concurrent_fragment_downloads": YTDLP_CONCURRENT_FRAGMENTS,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 20,
        "max_filesize": MAX_SEND_BYTES,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        },
    }

    cookies_file = getattr(config, "cookies_file", None)
    if isinstance(cookies_file, str) and cookies_file.strip():
        if os.path.exists(cookies_file.strip()):
            ydl_opts["cookiefile"] = cookies_file.strip()

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=True)


def _send_video_no_reply(
    chat_id: int,
    message_thread_id: Optional[int],
    file_path: str,
    caption_html: str,
) -> None:
    size = os.path.getsize(file_path)
    if size > MAX_SEND_BYTES:
        raise RuntimeError("File too large for Telegram bot upload limit")

    kwargs: Dict[str, Any] = {
        "supports_streaming": True,
        "disable_notification": True,
        "caption": caption_html,
        "parse_mode": "HTML",
    }
    if isinstance(message_thread_id, int):
        kwargs["message_thread_id"] = message_thread_id

    with open(file_path, "rb") as f:
        _bot_call(bot.send_video, chat_id, f, **kwargs)


# =========================
# Source detection
# =========================

def _detect_source(url: str) -> str:
    try:
        host = (urlparse(url).netloc or "").lower()
        host = host.replace("www.", "").strip()

        mapping = [
            (("youtube.com", "youtu.be", "m.youtube.com"), "YouTube"),
            (("instagram.com", "instagr.am"), "Instagram"),
            (("tiktok.com",), "TikTok"),
            (("vk.com", "vkvideo.ru"), "VK"),
            (("twitter.com", "x.com"), "X"),
            (("facebook.com", "fb.watch"), "Facebook"),
            (("t.me",), "Telegram"),
        ]

        for domains, name in mapping:
            if any(host == d or host.endswith("." + d) for d in domains):
                return name

        if host:
            return host
        return "Unknown"
    except Exception:
        return "Unknown"


def _format_sender_name(message) -> str:
    user = message.from_user
    first = (getattr(user, "first_name", "") or "").strip()
    last = (getattr(user, "last_name", "") or "").strip()

    full = f"{first} {last}".strip()
    if full:
        return full

    username = getattr(user, "username", None)
    if username:
        return f"@{username}"

    return str(getattr(user, "id", ""))


def _html_escape_text(s: str) -> str:
    return html.escape(s or "", quote=False)


def _html_escape_attr(s: str) -> str:
    # for href="...": escape quotes too
    return html.escape(s or "", quote=True)


# =========================
# Persistent prefs (JSON)
# =========================

_prefs_lock = threading.RLock()
_prefs_cache: Dict[str, Any] = {}


def _ensure_prefs_loaded() -> None:
    global _prefs_cache

    os.makedirs(DATA_DIR, exist_ok=True)

    with _prefs_lock:
        if _prefs_cache:
            return

        if not os.path.exists(PREFS_PATH):
            _prefs_cache = {"version": 1, "opt_out": {}}
            _save_prefs_locked()
            return

        try:
            with open(PREFS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("prefs not dict")
            if "opt_out" not in data or not isinstance(data.get("opt_out"), dict):
                data["opt_out"] = {}
            _prefs_cache = data
        except Exception:
            _prefs_cache = {"version": 1, "opt_out": {}}
            _save_prefs_locked()


def _save_prefs_locked() -> None:
    tmp = PREFS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_prefs_cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PREFS_PATH)


def _is_opted_out(chat_id: int, user_id: int) -> bool:
    _ensure_prefs_loaded()
    with _prefs_lock:
        opt_out = _prefs_cache.get("opt_out", {})
        chat_key = str(chat_id)
        users = opt_out.get(chat_key, {})
        return bool(users.get(str(user_id), False))


def _toggle_opt_out(chat_id: int, user_id: int) -> bool:
    """
    Returns new state:
    True  -> opted out (auto disabled, needs mention)
    False -> auto enabled
    """
    _ensure_prefs_loaded()
    with _prefs_lock:
        opt_out = _prefs_cache.setdefault("opt_out", {})
        chat_key = str(chat_id)
        users = opt_out.setdefault(chat_key, {})

        user_key = str(user_id)
        new_value = not bool(users.get(user_key, False))
        users[user_key] = new_value

        if not new_value:
            users.pop(user_key, None)
        if isinstance(users, dict) and not users:
            opt_out.pop(chat_key, None)

        _save_prefs_locked()
        return new_value


# =========================
# Mention detection
# =========================

def _get_bot_username_lower() -> str:
    try:
        me = _bot_call(bot.get_me)
        username = (getattr(me, "username", "") or "").strip()
        return username.lower()
    except Exception:
        return ""


def _get_bot_id() -> int:
    try:
        me = _bot_call(bot.get_me)
        return int(getattr(me, "id", 0) or 0)
    except Exception:
        return 0


BOT_USERNAME_LOWER = _get_bot_username_lower()
BOT_ID = _get_bot_id()


def _contains_bot_mention(text: str) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    if not BOT_USERNAME_LOWER:
        return False
    return f"@{BOT_USERNAME_LOWER}" in text.lower()


def _contains_sender_self_mention_or_me(text: str, sender_username: Optional[str]) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False

    t = text.lower()

    if re.search(r"(^|\s)me(\s|$)", t):
        return True

    if re.search(r"(^|\s)я(\s|$)", t):
        return True

    if sender_username:
        return re.search(rf"@{re.escape(sender_username.lower())}\b", t) is not None

    return False


# =========================
# Help / About text
# =========================

def _help_text_html(is_group: bool) -> str:
    bot_mention = f"@{BOT_USERNAME_LOWER}" if BOT_USERNAME_LOWER else "@<bot>"

    if is_group:
        usage = (
            "<b>Как пользоваться</b>\n"
            "• Просто отправляйте ссылку на видео в группу — бот скачает видео, отправит его и затем удалит исходную ссылку.\n"
            "• Подпись под видео: кликабельная «Ссылка на видео …» + «От Имя Фамилия».\n\n"
            "<b>Как отключить авто-скачивание для себя</b>\n"
            f"• Напишите в группе: {bot_mention} @ВашНик\n"
            "  (можно также написать «me» или «я» вместо ника)\n"
            "• Повторите команду — включится обратно.\n\n"
            "<b>Режим вручную (когда Вы отключились ясно)</b>\n"
            f"• Чтобы скачать: {bot_mention} <ссылка>\n"
        )
    else:
        usage = (
            "<b>Я бот для групп</b>\n"
            "Я скачиваю видео по ссылкам (YouTube/Instagram/TikTok/VK/X/Facebook/Telegram и др.) и публикую видео в группе.\n\n"
            "<b>Что нужно сделать</b>\n"
            "1) Добавьте меня в группу.\n"
            "2) Дайте права администратора и разрешение удалять сообщения.\n\n"
            "<b>Как работает по умолчанию</b>\n"
            "• Любая ссылка на видео в группе → скачивание → отправка видео → удаление исходной ссылки.\n\n"
            "<b>Как отключить авто-скачивание для себя</b>\n"
            f"• В группе напишите: {bot_mention} @ВашНик\n"
            "  (или «me» / «я»)\n"
            "• Повторите — включится обратно.\n\n"
            "<b>Когда авто отключено</b>\n"
            f"• Скачивание только так: {bot_mention} <ссылка>\n"
        )

    author = _html_escape_text(AUTHOR_NAME)
    repo_attr = _html_escape_attr(REPO_URL)

    return (
        f"{usage}\n"
        f"Автор: {author}\n"
        f'<a href="{repo_attr}">Ссылка на репозиторий</a>'
    )


def _try_set_commands() -> None:
    try:
        commands = [
            telebot.types.BotCommand("start", "Инструкция"),
            telebot.types.BotCommand("help", "Инструкция"),
        ]
        _bot_call(bot.set_my_commands, commands)
    except Exception:
        pass


_try_set_commands()


# =========================
# Jobs
# =========================

@dataclass(frozen=True)
class Job:
    chat_id: int
    message_thread_id: Optional[int]
    original_message_id: int
    url: str
    prefix: str
    source_name: str
    sender_full_name: str
    notify_on_fail: bool
    delete_original_on_success: bool


def _process_job(job_dict: Dict[str, Any]) -> None:
    job = Job(**job_dict)

    output_folder = getattr(config, "output_folder", "/tmp/yt-dlp-telegram") or "/tmp/yt-dlp-telegram"

    try:
        info = _download_with_ytdlp(job.url, job.prefix, output_folder)
        file_path = _find_downloaded_file(info, job.prefix, output_folder)

        if not file_path or not os.path.exists(file_path):
            raise RuntimeError("Downloaded file not found")

        # Make the first line clickable (HTML link) to the original URL
        url_attr = _html_escape_attr(job.url)
        source_text = _html_escape_text(job.source_name)
        sender_text = _html_escape_text(job.sender_full_name)

        caption_html = f'<a href="{url_attr}">Ссылка на видео {source_text}</a>\nОт {sender_text}'

        _send_video_no_reply(
            chat_id=job.chat_id,
            message_thread_id=job.message_thread_id,
            file_path=file_path,
            caption_html=caption_html,
        )

        if job.delete_original_on_success:
            _safe_delete_message(job.chat_id, job.original_message_id)

    except Exception:
        if job.notify_on_fail:
            _safe_send_message(job.chat_id, "Не удалось скачать", message_thread_id=job.message_thread_id)
    finally:
        _cleanup_files(job.prefix, output_folder)


def _worker_loop() -> None:
    while True:
        job = jobs_q.get()
        try:
            _process_job(job)
        finally:
            jobs_q.task_done()


for _ in range(WORKERS):
    t = threading.Thread(target=_worker_loop, daemon=True)
    t.start()


# =========================
# Service: bot added to group
# =========================

@bot.message_handler(content_types=["new_chat_members"])
def handle_new_chat_members(message):
    try:
        if message.chat.type not in ("group", "supergroup"):
            return

        new_members = getattr(message, "new_chat_members", None) or []
        if not new_members:
            return

        is_me = False
        for u in new_members:
            try:
                uid = int(getattr(u, "id", 0) or 0)
            except Exception:
                uid = 0
            uname = (getattr(u, "username", "") or "").lower()
            if (BOT_ID and uid == BOT_ID) or (BOT_USERNAME_LOWER and uname == BOT_USERNAME_LOWER):
                is_me = True
                break

        if not is_me:
            return

        chat_id = int(message.chat.id)
        message_thread_id = getattr(message, "message_thread_id", None)

        _safe_send_message_html(chat_id, _help_text_html(is_group=True), message_thread_id=message_thread_id)
    except Exception:
        pass


# =========================
# Private: /start, /help, any text
# =========================

@bot.message_handler(commands=["start", "help"])
def handle_start_help(message):
    try:
        chat_type = getattr(message.chat, "type", "")
        chat_id = int(message.chat.id)
        message_thread_id = getattr(message, "message_thread_id", None)

        if chat_type == "private":
            _safe_send_message_html(chat_id, _help_text_html(is_group=False), message_thread_id=message_thread_id)
            return

        if chat_type in ("group", "supergroup"):
            _safe_send_message_html(chat_id, _help_text_html(is_group=True), message_thread_id=message_thread_id)
            return
    except Exception:
        pass


@bot.message_handler(func=lambda m: getattr(m.chat, "type", "") == "private", content_types=["text"])
def handle_private_any_text(message):
    try:
        chat_id = int(message.chat.id)
        message_thread_id = getattr(message, "message_thread_id", None)
        _safe_send_message_html(chat_id, _help_text_html(is_group=False), message_thread_id=message_thread_id)
    except Exception:
        pass


# =========================
# Main handler
# =========================

@bot.message_handler(func=lambda m: True, content_types=["text", "photo", "video", "document", "audio", "voice"])
def handle_group_messages(message):
    try:
        if message.chat.type not in ("group", "supergroup"):
            return

        if getattr(message.from_user, "is_bot", False):
            return

        text = message.text if message.text else (message.caption if message.caption else "")
        if not isinstance(text, str) or not text.strip():
            return

        if text.strip().startswith("/"):
            return

        chat_id = int(message.chat.id)
        user_id = int(message.from_user.id)
        message_thread_id = getattr(message, "message_thread_id", None)
        bot_mentioned = _contains_bot_mention(text)

        # Toggle opt-out (self only)
        sender_username = getattr(message.from_user, "username", None)
        if bot_mentioned and _contains_sender_self_mention_or_me(text, sender_username):
            new_opt_out = _toggle_opt_out(chat_id, user_id)

            if sender_username:
                who = f"@{sender_username}"
            else:
                who = _format_sender_name(message)

            if new_opt_out:
                msg = (
                    f"{who}, теперь для Вас авто-скачивание отключено.\n"
                    f"Чтобы скачать видео, упоминайте бота и вставляйте ссылку: @"
                    f"{BOT_USERNAME_LOWER} <ссылка>"
                )
            else:
                msg = (
                    f"{who}, теперь для Вас включена автоотправка видео без упоминания бота.\n"
                    f"Можно просто отправлять ссылки."
                )

            _safe_send_message(chat_id, msg, message_thread_id=message_thread_id)
            return

        url = _extract_first_url(text)
        if not url:
            return

        url_info = urlparse(url)
        if not url_info.scheme or not url_info.netloc:
            return

        _log_request(message, url)

        opted_out = _is_opted_out(chat_id, user_id)

        # If user opted out -> only handle explicit mention with URL
        if opted_out and not bot_mentioned:
            return

        # Fail behavior:
        # - auto: silent
        # - explicit mention (when opted out): notify "Не удалось скачать" (silent)
        notify_on_fail = bool(opted_out and bot_mentioned)

        job = {
            "chat_id": chat_id,
            "message_thread_id": message_thread_id if isinstance(message_thread_id, int) else None,
            "original_message_id": int(message.message_id),
            "url": url,
            "prefix": uuid.uuid4().hex[:18],
            "source_name": _detect_source(url),
            "sender_full_name": _format_sender_name(message),
            "notify_on_fail": notify_on_fail,
            "delete_original_on_success": True,
        }

        try:
            jobs_q.put_nowait(job)
        except queue.Full:
            if notify_on_fail:
                _safe_send_message(chat_id, "Не удалось скачать", message_thread_id=message_thread_id)

    except Exception:
        pass


bot.infinity_polling(timeout=30, long_polling_timeout=30)
