from __future__ import annotations

import html
import logging
import queue
import re
import signal
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import telebot

from app.download_backend import MediaMetadata, download_metadata, extract_metadata, find_downloaded_file
from app.i18n import tr
from app.jobs import Flight, FlightCoordinator, Job
from app.logging_setup import configure_logging
from app.media_cache import DiskMediaCache
from app.settings import Settings, load_settings
from app.storage import Storage
from app.url_security import UnsafeUrlError, normalized_url_key, safe_url_for_log, validate_public_url

REPO_URL = "https://github.com/Avazbek22/LinkDownloaderBotForGroups"
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def extract_first_url(text: str) -> str | None:
    match = URL_RE.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip(").,;:!?]}>\"'")


class BotApplication:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.log = logging.getLogger("link_downloader_bot.app")
        self.storage = Storage(settings.data_dir, settings.default_language, settings.delete_original)
        self.storage.prune_media_cache(settings.file_id_cache_ttl_days, settings.file_id_cache_max_items)
        self.disk_cache = DiskMediaCache(
            settings.output_dir,
            max_files=settings.disk_cache_max_files,
            ttl_seconds=settings.disk_cache_ttl,
        )
        self.coordinator = FlightCoordinator()
        self.queue: queue.Queue[Flight | None] = queue.Queue(maxsize=settings.max_queue)
        self.stop_event = threading.Event()
        self.upload_slots = threading.BoundedSemaphore(settings.upload_workers)
        self.bot = telebot.TeleBot(settings.token, threaded=True)
        self.bot_id = 0
        self.bot_username = ""
        self.workers: list[threading.Thread] = []
        self.maintenance_thread: threading.Thread | None = None
        self._register_handlers()

    @property
    def mention(self) -> str:
        return f"@{self.bot_username}" if self.bot_username else "@bot"

    def initialize_identity(self) -> None:
        last_error: Exception | None = None
        for attempt in range(1, 6):
            try:
                me = self.bot.get_me()
                self.bot_id = int(me.id)
                self.bot_username = str(me.username or "").lower()
                return
            except Exception as exc:  # Telegram errors vary by transport/version
                last_error = exc
                self.log.warning("get_me failed attempt=%s", attempt, exc_info=True)
                if attempt < 5:
                    time.sleep(min(attempt * 2, 8))
        raise RuntimeError("cannot initialize Telegram bot identity") from last_error

    def start(self) -> None:
        self.disk_cache.maintain()
        if self.settings.cookies_file and not self.settings.cookies_file.is_file():
            self.log.warning("cookies file does not exist path=%s", self.settings.cookies_file)
        self.initialize_identity()
        self._set_commands()
        for index in range(self.settings.workers):
            thread = threading.Thread(target=self._worker, name=f"download-worker-{index + 1}", daemon=True)
            thread.start()
            self.workers.append(thread)
        self.maintenance_thread = threading.Thread(
            target=self._maintenance_loop,
            name="cache-maintenance",
            daemon=True,
        )
        self.maintenance_thread.start()
        self.log.info(
            "bot started username=%s workers=%s uploads=%s",
            self.bot_username,
            self.settings.workers,
            self.settings.upload_workers,
        )
        self.bot.infinity_polling(timeout=30, long_polling_timeout=30, allowed_updates=None)

    def stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        self.bot.stop_polling()
        for _ in self.workers:
            try:
                self.queue.put_nowait(None)
            except queue.Full:
                break
        for thread in self.workers:
            thread.join(timeout=10)
        if self.maintenance_thread is not None:
            self.maintenance_thread.join(timeout=2)
        self.disk_cache.maintain()
        self.log.info("bot stopped")

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                flight = self.queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                if flight is None:
                    return
                self._process_flight(flight)
            except Exception:
                self.log.exception("uncaught worker error")
                self._operator_alert("A download worker failed unexpectedly. Check bot.log.")
                if flight is not None:
                    self.coordinator.abort(flight)
            finally:
                self.queue.task_done()

    def _maintenance_loop(self) -> None:
        interval = min(60, self.settings.disk_cache_ttl)
        while not self.stop_event.wait(interval):
            try:
                self.disk_cache.maintain()
                self.storage.prune_media_cache(
                    self.settings.file_id_cache_ttl_days,
                    self.settings.file_id_cache_max_items,
                )
            except Exception:
                self.log.exception("cache maintenance failed")

    def _process_flight(self, flight: Flight) -> None:
        first = flight.jobs[0]
        started = time.monotonic()
        self.log.info("job metadata job_id=%s url=%s", first.job_id, safe_url_for_log(first.url))
        cache_profile = f"mp4:{self.settings.max_filesize}"
        cached = (
            self.storage.get_cached_by_url(f"{first.url_key}|{cache_profile}", self.settings.file_id_cache_ttl_days)
            if self.settings.media_cache_enabled
            else None
        )
        retry_jobs: list[Job] = []
        if cached is not None:
            media_key, file_id, source_name = cached
            if not self.coordinator.promote(flight, media_key):
                return
            metadata = MediaMetadata(first.url, {"id": media_key}, media_key, source_name)
            while True:
                batch = self.coordinator.pending(flight)
                if batch:
                    retry_jobs = self._send_by_file_id(batch, file_id, metadata, media_key)
                    if retry_jobs:
                        break
                if self.coordinator.finish_if_idle(flight):
                    self.log.info("Telegram cache hit job_id=%s media_key=%s", first.job_id, media_key)
                    return

        try:
            validate_public_url(first.url)
            metadata = extract_metadata(first.url, self.settings.cookies_file)
            final_url = metadata.info.get("webpage_url")
            if isinstance(final_url, str):
                validate_public_url(final_url)
        except Exception:
            self.log.exception("metadata failed job_id=%s url=%s", first.job_id, safe_url_for_log(first.url))
            self.coordinator.abort(flight)
            return

        media_key = f"{metadata.media_key}:mp4:{self.settings.max_filesize}"
        if cached is None and not self.coordinator.promote(flight, media_key):
            self.log.info("job joined media flight job_id=%s media_key=%s", first.job_id, media_key)
            return

        file_id = None
        if self.settings.media_cache_enabled:
            file_id = self.storage.get_file_id(media_key, self.settings.file_id_cache_ttl_days)
        file_path = self.disk_cache.get(media_key) if self.settings.media_cache_enabled else None

        while True:
            batch = retry_jobs or self.coordinator.pending(flight)
            retry_jobs = []
            if batch:
                if file_id:
                    retry = self._send_by_file_id(batch, file_id, metadata, media_key)
                    if retry:
                        file_id = None
                        if file_path is None:
                            file_path = self._obtain_file(metadata, media_key)
                        file_id = self._send_from_file(
                            retry,
                            file_path,
                            metadata,
                            media_key,
                            {f"{key}|{cache_profile}" for key in flight.url_keys},
                        )
                else:
                    if file_path is None:
                        file_path = self._obtain_file(metadata, media_key)
                    file_id = self._send_from_file(
                        batch,
                        file_path,
                        metadata,
                        media_key,
                        {f"{key}|{cache_profile}" for key in flight.url_keys},
                    )
            if self.coordinator.finish_if_idle(flight):
                break

        self.disk_cache.maintain()
        self.log.info(
            "job complete job_id=%s media_key=%s elapsed=%.2f",
            first.job_id,
            media_key,
            time.monotonic() - started,
        )

    def _obtain_file(self, metadata: MediaMetadata, media_key: str) -> Path:
        cached = self.disk_cache.get(media_key)
        if cached is not None:
            self.log.info("disk cache hit media_key=%s", media_key)
            return cached
        prefix = self.disk_cache.prefix(media_key)
        info = download_metadata(
            metadata,
            prefix,
            self.settings.output_dir,
            max_send_bytes=self.settings.max_filesize,
            concurrent_fragments=self.settings.concurrent_fragments,
            cookie_file=self.settings.cookies_file,
            deadline=time.monotonic() + self.settings.job_timeout,
        )
        path = find_downloaded_file(info, prefix, self.settings.output_dir)
        if path is None or not path.is_file():
            self.disk_cache.remove_prefix_except(prefix)
            raise RuntimeError("downloaded file not found")
        if path.stat().st_size > self.settings.max_filesize:
            self.disk_cache.remove_prefix_except(prefix)
            raise RuntimeError("downloaded file exceeds MAX_FILESIZE")
        self.disk_cache.remove_prefix_except(prefix, keep=path)
        self.disk_cache.maintain()
        self.log.info("download complete media_key=%s bytes=%s", media_key, path.stat().st_size)
        return path

    def _send_by_file_id(
        self,
        jobs: list[Job],
        file_id: str,
        metadata: MediaMetadata,
        media_key: str,
    ) -> list[Job]:
        for index, job in enumerate(jobs):
            try:
                self._send_video(job, file_id, metadata, upload=False)
                self._after_success(job)
            except Exception as exc:
                if self._is_invalid_file_id(exc):
                    self.storage.remove_file_id(media_key)
                    self.log.warning("invalid Telegram file_id media_key=%s", media_key)
                    return jobs[index:]
                self.log.exception("cached send failed job_id=%s chat_id=%s", job.job_id, job.chat_id)
        return []

    def _send_from_file(
        self,
        jobs: list[Job],
        path: Path,
        metadata: MediaMetadata,
        media_key: str,
        url_keys: set[str],
    ) -> str | None:
        file_id: str | None = None
        for job in jobs:
            try:
                if file_id:
                    self._send_video(job, file_id, metadata, upload=False)
                else:
                    response = self._send_video(job, path, metadata, upload=True)
                    video = getattr(response, "video", None)
                    candidate = getattr(video, "file_id", None)
                    if isinstance(candidate, str) and candidate:
                        file_id = candidate
                        if self.settings.media_cache_enabled:
                            self.storage.put_file_id(
                                media_key,
                                file_id,
                                self.settings.file_id_cache_max_items,
                                source_name=metadata.source_name,
                                url_keys=url_keys,
                            )
                self._after_success(job)
            except Exception:
                self.log.exception("video send failed job_id=%s chat_id=%s", job.job_id, job.chat_id)
        return file_id

    def _send_video(self, job: Job, video: Path | str, metadata: MediaMetadata, *, upload: bool) -> Any:
        language = self.storage.chat_language(job.chat_id)
        caption = tr(
            language,
            "caption",
            url=html.escape(job.url, quote=True),
            source=html.escape(metadata.source_name, quote=False),
            sender=html.escape(job.sender_name, quote=False),
        )
        kwargs: dict[str, Any] = {
            "chat_id": job.chat_id,
            "caption": caption,
            "parse_mode": "HTML",
            "supports_streaming": True,
            "disable_notification": True,
        }
        if job.message_thread_id is not None:
            kwargs["message_thread_id"] = job.message_thread_id
        if upload:
            with self.upload_slots, Path(video).open("rb") as handle:
                return self.bot.send_video(video=handle, **kwargs)
        return self.bot.send_video(video=video, **kwargs)

    def _after_success(self, job: Job) -> None:
        if job.delete_original:
            try:
                self.bot.delete_message(job.chat_id, job.original_message_id)
            except Exception:
                self.log.exception("original delete failed job_id=%s chat_id=%s", job.job_id, job.chat_id)

    @staticmethod
    def _is_invalid_file_id(exc: Exception) -> bool:
        text = str(exc).lower()
        return "file_id" in text or "file identifier" in text or "file reference" in text

    def _safe_message(self, chat_id: int, text: str, thread_id: int | None = None, *, html_mode: bool = False) -> bool:
        try:
            kwargs: dict[str, Any] = {
                "disable_notification": True,
                "disable_web_page_preview": True,
            }
            if html_mode:
                kwargs["parse_mode"] = "HTML"
            if thread_id is not None:
                kwargs["message_thread_id"] = thread_id
            self.bot.send_message(chat_id, text, **kwargs)
            return True
        except Exception:
            self.log.exception("message send failed chat_id=%s", chat_id)
            return False

    def _operator_alert(self, text: str) -> None:
        if self.settings.logs_chat_id is None:
            return
        try:
            self.bot.send_message(
                self.settings.logs_chat_id,
                text,
                disable_notification=True,
                disable_web_page_preview=True,
            )
        except Exception:
            self.log.exception("operator alert failed")

    def _is_admin(self, chat_id: int, user_id: int) -> bool:
        try:
            member = self.bot.get_chat_member(chat_id, user_id)
            return str(getattr(member, "status", "")).lower() in {"administrator", "creator"}
        except Exception:
            self.log.exception("admin check failed chat_id=%s user_id=%s", chat_id, user_id)
            return False

    def _admin_hint(self, chat_id: int, language: str) -> str:
        try:
            member = self.bot.get_chat_member(chat_id, self.bot_id)
            if str(getattr(member, "status", "")).lower() in {"administrator", "creator"} and bool(
                getattr(member, "can_delete_messages", True)
            ):
                return ""
        except Exception:
            self.log.exception("bot permission check failed chat_id=%s", chat_id)
            return ""
        return tr(language, "admin_hint")

    def _help(self, chat_id: int, private: bool) -> str:
        language = self.storage.chat_language(chat_id)
        if private:
            return tr(language, "private_help", repo_url=html.escape(REPO_URL, quote=True))
        return tr(language, "group_help", bot_mention=html.escape(self.mention, quote=False))

    def _set_commands(self) -> None:
        try:
            self.bot.set_my_commands(
                [
                    telebot.types.BotCommand("start", "Show instructions"),
                    telebot.types.BotCommand("help", "Show instructions"),
                    telebot.types.BotCommand("language", "Change language (admins)"),
                    telebot.types.BotCommand("settings", "Show group settings"),
                    telebot.types.BotCommand("delete_original", "Configure link deletion (admins)"),
                ]
            )
        except Exception:
            self.log.exception("cannot set Telegram commands")

    def _register_handlers(self) -> None:
        bot = self.bot

        @bot.message_handler(content_types=["new_chat_members"])
        def new_members(message: Any) -> None:
            if getattr(message.chat, "type", "") not in {"group", "supergroup"}:
                return
            members = getattr(message, "new_chat_members", None) or []
            if not any(int(getattr(member, "id", 0) or 0) == self.bot_id for member in members):
                return
            chat_id = int(message.chat.id)
            if self.storage.was_welcomed("group", chat_id):
                return
            language = self.storage.chat_language(chat_id)
            text = self._admin_hint(chat_id, language) + self._help(chat_id, private=False)
            if self._safe_message(chat_id, text, getattr(message, "message_thread_id", None), html_mode=True):
                self.storage.mark_welcomed("group", chat_id)

        @bot.message_handler(commands=["start", "help"])
        def start_help(message: Any) -> None:
            chat_type = getattr(message.chat, "type", "")
            chat_id = int(message.chat.id)
            private = chat_type == "private"
            if not private and chat_type not in {"group", "supergroup"}:
                return
            self._safe_message(
                chat_id,
                self._help(chat_id, private=private),
                getattr(message, "message_thread_id", None),
                html_mode=True,
            )
            if private:
                self.storage.mark_welcomed("private", int(message.from_user.id))

        @bot.message_handler(commands=["language"])
        def language(message: Any) -> None:
            chat_id = int(message.chat.id)
            current = self.storage.chat_language(chat_id)
            parts = (message.text or "").split()
            if len(parts) == 1:
                self._safe_message(chat_id, tr(current, "language_current", language=current))
                return
            requested = parts[1].lower()
            if requested not in {"en", "ru"}:
                self._safe_message(chat_id, tr(current, "language_invalid"))
                return
            if getattr(message.chat, "type", "") != "private" and not self._is_admin(
                chat_id, int(message.from_user.id)
            ):
                self._safe_message(chat_id, tr(current, "language_admin_only"))
                return
            self.storage.set_chat_language(chat_id, requested)
            self._safe_message(chat_id, tr(requested, "language_changed"))

        @bot.message_handler(commands=["settings"])
        def group_settings(message: Any) -> None:
            chat_id = int(message.chat.id)
            language_code = self.storage.chat_language(chat_id)
            if getattr(message.chat, "type", "") not in {"group", "supergroup"}:
                return
            if not self._is_admin(chat_id, int(message.from_user.id)):
                self._safe_message(chat_id, tr(language_code, "admin_only"))
                return
            enabled = self.storage.delete_original(chat_id)
            state = tr(language_code, "state_on" if enabled else "state_off")
            self._safe_message(
                chat_id,
                tr(
                    language_code,
                    "settings_summary",
                    language=language_code,
                    delete_original=state,
                ),
                getattr(message, "message_thread_id", None),
                html_mode=True,
            )

        @bot.message_handler(commands=["delete_original"])
        def delete_original(message: Any) -> None:
            chat_id = int(message.chat.id)
            language_code = self.storage.chat_language(chat_id)
            if getattr(message.chat, "type", "") not in {"group", "supergroup"}:
                return
            if not self._is_admin(chat_id, int(message.from_user.id)):
                self._safe_message(chat_id, tr(language_code, "admin_only"))
                return
            parts = (message.text or "").split()
            if len(parts) != 2 or parts[1].lower() not in {"on", "off"}:
                self._safe_message(chat_id, tr(language_code, "delete_usage"))
                return
            enabled = parts[1].lower() == "on"
            self.storage.set_delete_original(chat_id, enabled)
            state = tr(language_code, "state_on" if enabled else "state_off")
            self._safe_message(chat_id, tr(language_code, "delete_changed", state=state))

        @bot.message_handler(func=lambda item: getattr(item.chat, "type", "") == "private", content_types=["text"])
        def private_text(message: Any) -> None:
            chat_id = int(message.chat.id)
            user_id = int(message.from_user.id)
            if not self.storage.was_welcomed("private", user_id):
                if self._safe_message(chat_id, self._help(chat_id, private=True), html_mode=True):
                    self.storage.mark_welcomed("private", user_id)
                return
            self._safe_message(chat_id, tr(self.storage.chat_language(chat_id), "private_hint"))

        @bot.message_handler(
            func=lambda _message: True,
            content_types=["text", "photo", "video", "document", "audio", "voice"],
        )
        def group_message(message: Any) -> None:
            self._handle_group_message(message)

    def _handle_group_message(self, message: Any) -> None:
        try:
            if getattr(message.chat, "type", "") not in {"group", "supergroup"}:
                return
            if getattr(message.from_user, "is_bot", False):
                return
            text = message.text or message.caption or ""
            if not text.strip() or text.lstrip().startswith("/"):
                return
            chat_id = int(message.chat.id)
            user_id = int(message.from_user.id)
            mentioned = bool(self.bot_username and re.search(rf"(^|\s)@{re.escape(self.bot_username)}\b", text.lower()))
            username = getattr(message.from_user, "username", None)
            if mentioned and self._self_mention(text, username):
                opted_out = self.storage.toggle_opt_out(chat_id, user_id)
                who = f"@{username}" if username else self._sender_name(message)
                key = "opted_out" if opted_out else "opted_in"
                self._safe_message(
                    chat_id,
                    tr(self.storage.chat_language(chat_id), key, who=who, bot_mention=self.mention),
                    getattr(message, "message_thread_id", None),
                )
                return
            url = extract_first_url(text)
            if url is None:
                return
            if self.storage.is_opted_out(chat_id, user_id) and not mentioned:
                return
            validate_public_url(url)
            job = Job(
                job_id=uuid.uuid4().hex[:16],
                chat_id=chat_id,
                message_thread_id=(
                    int(message.message_thread_id)
                    if isinstance(getattr(message, "message_thread_id", None), int)
                    else None
                ),
                original_message_id=int(message.message_id),
                user_id=user_id,
                url=url,
                url_key=normalized_url_key(url),
                sender_name=self._sender_name(message),
                delete_original=self.storage.delete_original(chat_id),
            )
            flight = self.coordinator.submit(job)
            if flight is None:
                self.log.info("job joined URL flight job_id=%s url=%s", job.job_id, safe_url_for_log(url))
                return
            try:
                self.queue.put_nowait(flight)
                self.log.info("job queued job_id=%s chat_id=%s url=%s", job.job_id, chat_id, safe_url_for_log(url))
            except queue.Full:
                self.coordinator.abort(flight)
                self.log.warning("queue full job_id=%s chat_id=%s", job.job_id, chat_id)
                self._operator_alert("The download queue is full. Check bot.log.")
        except UnsafeUrlError:
            self.log.warning("unsafe URL rejected chat_id=%s", getattr(message.chat, "id", None), exc_info=True)
        except Exception:
            self.log.exception("group handler failed chat_id=%s", getattr(message.chat, "id", None))

    @staticmethod
    def _self_mention(text: str, username: str | None) -> bool:
        lowered = text.lower()
        if re.search(r"(^|\s)(me|я)(\s|$)", lowered):
            return True
        return bool(username and re.search(rf"(^|\s)@{re.escape(username.lower())}\b", lowered))

    @staticmethod
    def _sender_name(message: Any) -> str:
        first = str(getattr(message.from_user, "first_name", "") or "").strip()
        last = str(getattr(message.from_user, "last_name", "") or "").strip()
        full = f"{first} {last}".strip()
        username = getattr(message.from_user, "username", None)
        return full or (f"@{username}" if username else str(message.from_user.id))


def main() -> None:
    settings = load_settings()
    configure_logging(settings.logs_dir, settings.log_level)
    application = BotApplication(settings)

    def shutdown(_signum: int, _frame: Any) -> None:
        application.stop()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
    try:
        application.start()
    finally:
        application.stop()


if __name__ == "__main__":
    main()
