from __future__ import annotations

import html
import logging
import queue
import re
import signal
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import telebot

from app.download_backend import (
    InstagramContentRestrictedError,
    MediaMetadata,
    display_source_name,
    download_metadata,
    extract_metadata,
    find_downloaded_file,
    has_downloadable_video,
)
from app.group_registry import ACTIVE_TELEGRAM_STATUSES, BLOCKED_ACCESS_STATUSES, GroupRegistry
from app.i18n import tr
from app.jobs import Flight, FlightCoordinator, Job
from app.logging_setup import configure_logging
from app.media_cache import DiskMediaCache
from app.settings import Settings, load_settings
from app.storage import Storage
from app.url_security import (
    UnsafeUrlError,
    normalized_url_key,
    safe_error_for_log,
    safe_url_for_log,
    validate_public_url,
)

REPO_URL = "https://github.com/Avazbek22/LinkDownloaderBotForGroups"
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
RETRYABLE_REACTIONS = frozenset({"👎", "🙈"})
FAILED_RETRY_TTL_SECONDS = 7 * 24 * 60 * 60
FAILED_RETRY_MAX_ITEMS = 1_000
GROUP_NOTIFICATION_RETRY_SECONDS = 5 * 60


@dataclass(frozen=True)
class FailedRetry:
    job: Job
    emoji: str
    failed_at: float


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
        self.group_registry = GroupRegistry(
            settings.data_dir,
            settings.group_owner_username,
            settings.group_access_mode,
        )
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
        self._failed_retry_lock = threading.RLock()
        self._failed_retries: OrderedDict[tuple[int, int], FailedRetry] = OrderedDict()
        self._retries_in_progress: set[tuple[int, int]] = set()
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
        self._refresh_group_registry()
        self._maintain_group_access()
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
        self.bot.infinity_polling(
            timeout=30,
            long_polling_timeout=30,
            allowed_updates=["message", "message_reaction", "my_chat_member", "callback_query"],
        )

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
        shutdown_deadline = time.monotonic() + 25
        for thread in self.workers:
            remaining = max(0.0, shutdown_deadline - time.monotonic())
            thread.join(timeout=min(10.0, remaining))
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
                    jobs = list(flight.jobs)
                    self.coordinator.abort(flight)
                    if flight.media_key is None:
                        self._after_probe_failure_many(jobs)
                    else:
                        self._after_failure_many(jobs)
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
            try:
                self._maintain_group_access()
            except Exception:
                self.log.exception("group access maintenance failed")

    def _process_flight(self, flight: Flight) -> None:
        first = flight.jobs[0]
        started = time.monotonic()
        deadline = started + self.settings.job_timeout
        self.log.info("job metadata job_id=%s url=%s", first.job_id, safe_url_for_log(first.url))
        # Bump the profile whenever delivery compatibility changes so an old,
        # already-uploaded Telegram file_id cannot bypass the new validation.
        cache_profile = f"mp4-h264-v2:{self.settings.max_filesize}"
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
            metadata = MediaMetadata(first.url, {"id": media_key}, media_key, display_source_name(source_name))
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
            metadata = extract_metadata(first.url, self.settings.cookies_file, deadline)
            final_url = metadata.info.get("webpage_url")
            if isinstance(final_url, str):
                validate_public_url(final_url)
        except InstagramContentRestrictedError:
            self.log.info(
                "link hidden by Instagram content controls job_id=%s url=%s",
                first.job_id,
                safe_url_for_log(first.url),
            )
            self._after_instagram_restriction_many(self.coordinator.abort(flight))
            return
        except Exception as exc:
            self.log.info(
                "link ignored after media probe job_id=%s url=%s error=%s detail=%s",
                first.job_id,
                safe_url_for_log(first.url),
                type(exc).__name__,
                safe_error_for_log(exc),
            )
            self._after_probe_failure_many(self.coordinator.abort(flight))
            return

        if not has_downloadable_video(metadata.info):
            self.log.info(
                "link ignored because no video was found job_id=%s url=%s",
                first.job_id,
                safe_url_for_log(first.url),
            )
            self._after_probe_failure_many(self.coordinator.abort(flight))
            return

        media_key = f"{metadata.media_key}:mp4-h264-v2:{self.settings.max_filesize}"
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
                            file_path = self._obtain_file(metadata, media_key, deadline)
                        file_id = self._send_from_file(
                            retry,
                            file_path,
                            metadata,
                            media_key,
                            {f"{key}|{cache_profile}" for key in flight.url_keys},
                        )
                else:
                    if file_path is None:
                        file_path = self._obtain_file(metadata, media_key, deadline)
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

    def _obtain_file(self, metadata: MediaMetadata, media_key: str, deadline: float) -> Path:
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
            deadline=deadline,
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
                self._after_failure(job)
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
                self._after_failure(job)
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
        self._forget_failed_retry(job)
        if job.delete_original:
            try:
                self.bot.delete_message(job.chat_id, job.original_message_id)
                return
            except Exception:
                self.log.exception("original delete failed job_id=%s chat_id=%s", job.job_id, job.chat_id)
        if not self._set_status_reaction(job, "👍"):
            self._clear_status_reaction(job)

    def _after_failure(self, job: Job) -> None:
        if self._set_status_reaction(job, "👎"):
            self._remember_failed_retry(job, "👎")
        else:
            self._forget_failed_retry(job)
            self._clear_status_reaction(job)

    def _after_failure_many(self, jobs: list[Job]) -> None:
        for job in jobs:
            self._after_failure(job)

    def _after_instagram_restriction(self, job: Job) -> None:
        if self._set_status_reaction(job, "🙈"):
            self._remember_failed_retry(job, "🙈")
        else:
            self._forget_failed_retry(job)
            self._clear_status_reaction(job)

    def _after_instagram_restriction_many(self, jobs: list[Job]) -> None:
        for job in jobs:
            self._after_instagram_restriction(job)

    def _clear_status_many(self, jobs: list[Job]) -> None:
        for job in jobs:
            self._clear_status_reaction(job)

    def _after_probe_failure_many(self, jobs: list[Job]) -> None:
        for job in jobs:
            if self._retry_in_progress(job):
                self._after_failure(job)
            else:
                self._clear_status_reaction(job)

    @staticmethod
    def _retry_key(job: Job) -> tuple[int, int]:
        return job.chat_id, job.original_message_id

    def _prune_failed_retries_locked(self, now: float) -> None:
        expired = [
            key for key, failed in self._failed_retries.items() if now - failed.failed_at > FAILED_RETRY_TTL_SECONDS
        ]
        for key in expired:
            self._failed_retries.pop(key, None)
            self._retries_in_progress.discard(key)
        while len(self._failed_retries) > FAILED_RETRY_MAX_ITEMS:
            key, _failed = self._failed_retries.popitem(last=False)
            self._retries_in_progress.discard(key)

    def _remember_failed_retry(self, job: Job, emoji: str) -> None:
        if not self.settings.status_reactions or emoji not in RETRYABLE_REACTIONS:
            return
        key = self._retry_key(job)
        now = time.monotonic()
        with self._failed_retry_lock:
            self._failed_retries.pop(key, None)
            self._failed_retries[key] = FailedRetry(job=job, emoji=emoji, failed_at=now)
            self._retries_in_progress.discard(key)
            self._prune_failed_retries_locked(now)

    def _forget_failed_retry(self, job: Job) -> None:
        key = self._retry_key(job)
        with self._failed_retry_lock:
            self._failed_retries.pop(key, None)
            self._retries_in_progress.discard(key)

    def _retry_in_progress(self, job: Job) -> bool:
        with self._failed_retry_lock:
            return self._retry_key(job) in self._retries_in_progress

    @staticmethod
    def _reaction_emojis(reactions: Any) -> set[str]:
        return {
            emoji
            for reaction in reactions or []
            if isinstance((emoji := getattr(reaction, "emoji", None)), str) and emoji
        }

    def _claim_failed_retry(self, update: Any) -> FailedRetry | None:
        if not self.settings.status_reactions:
            return None
        chat = getattr(update, "chat", None)
        user = getattr(update, "user", None)
        if getattr(chat, "type", "") not in {"group", "supergroup"}:
            return None
        if user is None or getattr(user, "is_bot", False):
            return None
        try:
            key = int(chat.id), int(update.message_id)
        except (AttributeError, TypeError, ValueError):
            return None
        if not self._record_group_access(chat):
            return None
        added = self._reaction_emojis(getattr(update, "new_reaction", None)) - self._reaction_emojis(
            getattr(update, "old_reaction", None)
        )
        now = time.monotonic()
        with self._failed_retry_lock:
            self._prune_failed_retries_locked(now)
            failed = self._failed_retries.get(key)
            if failed is None or failed.emoji not in added or key in self._retries_in_progress:
                return None
            self._retries_in_progress.add(key)
            return failed

    def _restore_failed_retry(self, job: Job) -> None:
        key = self._retry_key(job)
        with self._failed_retry_lock:
            self._retries_in_progress.discard(key)
            failed = self._failed_retries.get(key)
        if failed is not None and not self._set_status_reaction(job, failed.emoji):
            self._forget_failed_retry(job)
            self._clear_status_reaction(job)

    def _restore_or_clear_unqueued(self, jobs: list[Job]) -> None:
        for job in jobs:
            if self._retry_in_progress(job):
                self._restore_failed_retry(job)
            else:
                self._clear_status_reaction(job)

    def _handle_retry_reaction(self, update: Any) -> None:
        failed = self._claim_failed_retry(update)
        if failed is None:
            return
        retry_job = replace(failed.job, job_id=uuid.uuid4().hex[:16])
        flight: Flight | None = None
        queued = False
        try:
            if not self._set_status_reaction(retry_job, "👀"):
                self._clear_status_reaction(retry_job)
            flight = self.coordinator.submit(retry_job)
            if flight is None:
                self.log.info(
                    "reaction retry joined URL flight job_id=%s chat_id=%s url=%s",
                    retry_job.job_id,
                    retry_job.chat_id,
                    safe_url_for_log(retry_job.url),
                )
                return
            try:
                self.queue.put_nowait(flight)
                queued = True
                self.log.info(
                    "reaction retry queued job_id=%s chat_id=%s url=%s",
                    retry_job.job_id,
                    retry_job.chat_id,
                    safe_url_for_log(retry_job.url),
                )
            except queue.Full:
                self._restore_or_clear_unqueued(self.coordinator.abort(flight))
                self.log.warning("reaction retry queue full job_id=%s chat_id=%s", retry_job.job_id, retry_job.chat_id)
                self._operator_alert("The download queue is full. Check bot.log.")
        except Exception:
            if not queued:
                if flight is not None:
                    self.coordinator.abort(flight)
                self._restore_failed_retry(retry_job)
            self.log.exception(
                "reaction retry handler failed job_id=%s chat_id=%s",
                retry_job.job_id,
                retry_job.chat_id,
            )

    def _set_status_reaction(self, job: Job, emoji: str) -> bool:
        if not self.settings.status_reactions:
            return False
        method = getattr(self.bot, "set_message_reaction", None)
        if method is None:
            return False
        try:
            method(
                job.chat_id,
                job.original_message_id,
                [telebot.types.ReactionTypeEmoji(emoji=emoji)],
                is_big=False,
            )
            return True
        except Exception as exc:
            # Reactions may be disabled or restricted per chat. They are only
            # a best-effort status hint and must never affect the download.
            self.log.info(
                "status reaction unavailable job_id=%s chat_id=%s error=%s",
                job.job_id,
                job.chat_id,
                type(exc).__name__,
            )
            return False

    def _clear_status_reaction(self, job: Job) -> None:
        if not self.settings.status_reactions:
            return
        method = getattr(self.bot, "set_message_reaction", None)
        if method is None:
            return
        with suppress(Exception):
            method(job.chat_id, job.original_message_id, [], is_big=False)

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

    @property
    def _approval_required(self) -> bool:
        return self.settings.group_access_mode == "approval"

    @staticmethod
    def _chat_title(chat: Any) -> str | None:
        title = str(getattr(chat, "title", "") or "").strip()
        return title or None

    def _refresh_group_registry(self) -> None:
        """Reconcile every known ID with Telegram without granting implicit access."""
        configured_bootstrap = set(self.settings.group_bootstrap_chat_ids)
        known_ids = self.storage.known_group_ids() | configured_bootstrap
        for group in self.group_registry.all_groups():
            try:
                chat_id = int(group["chat_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if chat_id < 0:
                known_ids.add(chat_id)
        for chat_id in sorted(known_ids):
            existing = self.group_registry.get_group(chat_id) or {}
            try:
                member = self.bot.get_chat_member(chat_id, self.bot_id)
                telegram_status = str(getattr(member, "status", "") or "unknown")
            except Exception as exc:
                self.log.info(
                    "group membership verification failed chat_id=%s error=%s",
                    chat_id,
                    type(exc).__name__,
                )
                if chat_id in configured_bootstrap and not self.group_registry.bootstrap_completed(chat_id):
                    self.group_registry.record_bootstrap_result(chat_id, error=type(exc).__name__)
                else:
                    self.group_registry.record_presence(
                        chat_id,
                        title=existing.get("title"),
                        chat_type=existing.get("type"),
                        telegram_status="unknown",
                        approval_required=self._approval_required,
                    )
                continue

            title = existing.get("title")
            chat_type = existing.get("type")
            try:
                chat = self.bot.get_chat(chat_id)
                title = self._chat_title(chat) or title
                chat_type = str(getattr(chat, "type", "") or chat_type or "unknown")
            except Exception as exc:
                self.log.info("group metadata refresh failed chat_id=%s error=%s", chat_id, type(exc).__name__)

            if chat_id in configured_bootstrap and not self.group_registry.bootstrap_completed(chat_id):
                self.group_registry.record_bootstrap_result(
                    chat_id,
                    title=title,
                    chat_type=chat_type,
                    telegram_status=telegram_status,
                )
            else:
                self.group_registry.record_presence(
                    chat_id,
                    title=title,
                    chat_type=chat_type,
                    telegram_status=telegram_status,
                    approval_required=self._approval_required,
                )

    def _record_group_access(
        self,
        chat: Any,
        *,
        added_by: Any | None = None,
        membership_started: bool = False,
        telegram_status: str | None = None,
    ) -> bool:
        try:
            chat_id = int(chat.id)
        except (AttributeError, TypeError, ValueError):
            return False
        if telegram_status is None:
            existing = self.group_registry.get_group(chat_id) or {}
            known_status = str(existing.get("telegram_status") or "unknown")
            telegram_status = known_status if known_status in ACTIVE_TELEGRAM_STATUSES else "member"
        group = self.group_registry.record_presence(
            chat_id,
            title=self._chat_title(chat),
            chat_type=getattr(chat, "type", None),
            telegram_status=telegram_status,
            added_by=added_by,
            approval_required=self._approval_required,
            membership_started=membership_started,
        )
        allowed = self.group_registry.access_allowed(chat_id, self._approval_required)
        if not allowed and group.get("access_status") == "pending":
            self._send_pending_group_notice(group)
            self._flush_pending_group_notifications()
        return allowed

    def _send_pending_group_notice(self, group: dict[str, Any]) -> None:
        chat_id = int(group["chat_id"])
        if not self.group_registry.claim_group_notice(
            chat_id,
            retry_after_seconds=GROUP_NOTIFICATION_RETRY_SECONDS,
        ):
            return
        language = self.storage.chat_language(chat_id)
        success = self._safe_message(chat_id, tr(language, "group_pending_approval"))
        self.group_registry.mark_group_notice(chat_id, success)

    @staticmethod
    def _pending_markup(request_id: str) -> Any:
        markup = telebot.types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            telebot.types.InlineKeyboardButton("✅ Approve", callback_data=f"ga:a:{request_id}"),
            telebot.types.InlineKeyboardButton("⛔ Reject", callback_data=f"ga:r:{request_id}"),
        )
        return markup

    @staticmethod
    def _added_by_text(group: dict[str, Any]) -> str:
        actor = group.get("added_by")
        if not isinstance(actor, dict):
            return "Unknown (Telegram did not include the original event)"
        first = str(actor.get("first_name") or "").strip()
        last = str(actor.get("last_name") or "").strip()
        name = f"{first} {last}".strip() or "Unknown"
        username = str(actor.get("username") or "").strip()
        suffix = f"@{username}" if username else "no username"
        return f"{name} · {suffix} · ID {actor.get('id')}"

    def _send_pending_card(self, owner_id: int, group: dict[str, Any], *, track_delivery: bool) -> bool:
        request_id = group.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return False
        current = self.group_registry.get_group(int(group["chat_id"]))
        if (
            not isinstance(current, dict)
            or current.get("access_status") != "pending"
            or current.get("request_id") != request_id
        ):
            return False
        title = html.escape(str(group.get("title") or "Untitled group"), quote=False)
        actor = html.escape(self._added_by_text(group), quote=False)
        text = (
            "<b>New group approval request</b>\n\n"
            f"Group: <b>{title}</b>\n"
            f"Chat ID: <code>{int(group['chat_id'])}</code>\n"
            f"Telegram status: {html.escape(str(group.get('telegram_status') or 'unknown'), quote=False)}\n"
            f"Added by: {actor}\n\n"
            "Downloads are blocked until you approve this group."
        )
        success = False
        try:
            self.bot.send_message(
                owner_id,
                text,
                parse_mode="HTML",
                disable_notification=True,
                disable_web_page_preview=True,
                reply_markup=self._pending_markup(request_id),
            )
            success = True
        except Exception as exc:
            self.log.warning(
                "owner group notification failed chat_id=%s error=%s",
                group.get("chat_id"),
                type(exc).__name__,
            )
        if track_delivery:
            self.group_registry.mark_notification(request_id, success)
        return success

    def _flush_pending_group_notifications(self) -> None:
        owner_id = self.group_registry.owner_id()
        if owner_id is None:
            return
        for group in self.group_registry.claim_pending_notifications(
            retry_after_seconds=GROUP_NOTIFICATION_RETRY_SECONDS
        ):
            self._send_pending_card(owner_id, group, track_delivery=True)

    def _notify_group_approved(self, group: dict[str, Any]) -> None:
        if str(group.get("telegram_status")) not in ACTIVE_TELEGRAM_STATUSES:
            return
        chat_id = int(group["chat_id"])
        language = self.storage.chat_language(chat_id)
        prefix = tr(language, "group_approved")
        if self.storage.was_welcomed("group", chat_id):
            self._safe_message(chat_id, prefix)
            return
        text = f"{prefix}\n\n{self._admin_hint(chat_id, language)}{self._help(chat_id, private=False)}"
        if self._safe_message(chat_id, text, html_mode=True):
            self.storage.mark_welcomed("group", chat_id)

    def _welcome_group(self, chat_id: int, thread_id: int | None = None) -> None:
        if not self.group_registry.access_allowed(chat_id, self._approval_required):
            return
        if self.storage.was_welcomed("group", chat_id):
            return
        language = self.storage.chat_language(chat_id)
        text = self._admin_hint(chat_id, language) + self._help(chat_id, private=False)
        if self._safe_message(chat_id, text, thread_id, html_mode=True):
            self.storage.mark_welcomed("group", chat_id)

    def _leave_blocked_group(self, group: dict[str, Any], *, notify: bool) -> None:
        chat_id = int(group["chat_id"])
        if notify:
            language = self.storage.chat_language(chat_id)
            key = "group_rejected" if group.get("access_status") == "rejected" else "group_approval_expired"
            self._safe_message(chat_id, tr(language, key))
        try:
            result = self.bot.leave_chat(chat_id)
            if result is False:
                raise RuntimeError("Telegram returned false")
            self.group_registry.mark_leave_attempt(chat_id, None)
        except Exception as exc:
            self.group_registry.mark_leave_attempt(chat_id, type(exc).__name__)
            self.log.warning("cannot leave blocked group chat_id=%s error=%s", chat_id, type(exc).__name__)

    def _maintain_group_access(self, *, flush_notifications: bool = True) -> None:
        if not self._approval_required:
            return
        owner_id = self.group_registry.owner_id()
        if owner_id is not None:
            for group in self.group_registry.approve_pending_added_by(owner_id):
                self._notify_group_approved(group)
        newly_expired = self.group_registry.expire_pending(self.settings.pending_group_ttl_hours)
        for group in newly_expired:
            self._leave_blocked_group(group, notify=True)
        for group in self.group_registry.pending_groups():
            self._send_pending_group_notice(group)
        for group in self.group_registry.blocked_groups_for_leave(retry_after_seconds=GROUP_NOTIFICATION_RETRY_SECONDS):
            self._leave_blocked_group(group, notify=False)
        if flush_notifications:
            self._flush_pending_group_notifications()

    def _maybe_bind_owner(self, user: Any) -> bool:
        if not self._approval_required:
            return False
        try:
            user_id = int(user.id)
        except (AttributeError, TypeError, ValueError):
            return False
        result = self.group_registry.bind_owner(user_id, getattr(user, "username", None))
        if result not in {"claimed", "owner"}:
            return False
        if result == "claimed":
            self.log.info("group policy owner bound user_id=%s", user_id)
            self._safe_message(
                user_id,
                "Owner access is now bound to this Telegram account. Use /groups or /pending_groups.",
            )
            self._set_owner_commands(user_id)
        for group in self.group_registry.approve_pending_added_by(user_id):
            self._notify_group_approved(group)
        self._flush_pending_group_notifications()
        return True

    def _set_owner_commands(self, owner_id: int) -> None:
        try:
            self.bot.set_my_commands(
                [
                    telebot.types.BotCommand("start", "Show instructions"),
                    telebot.types.BotCommand("help", "Show instructions"),
                    telebot.types.BotCommand("en", "Switch to English (admins)"),
                    telebot.types.BotCommand("ru", "Переключить на русский (админы)"),
                    telebot.types.BotCommand("settings", "Show group settings"),
                    telebot.types.BotCommand("delete_original", "Configure link deletion (admins)"),
                    telebot.types.BotCommand("groups", "Show current bot groups"),
                    telebot.types.BotCommand("pending_groups", "Review pending groups"),
                ],
                scope=telebot.types.BotCommandScopeChat(chat_id=owner_id),
            )
        except Exception:
            self.log.exception("cannot set owner Telegram commands")

    @staticmethod
    def _group_report_line(group: dict[str, Any]) -> str:
        title = html.escape(str(group.get("title") or "Untitled group"), quote=False)
        chat_id = int(group.get("chat_id") or 0)
        chat_type = html.escape(str(group.get("type") or "unknown"), quote=False)
        telegram_status = html.escape(str(group.get("telegram_status") or "unknown"), quote=False)
        return f"• <b>{title}</b> — <code>{chat_id}</code> ({chat_type}; {telegram_status})"

    def _send_long_html(self, chat_id: int, sections: list[tuple[str, list[dict[str, Any]]]]) -> None:
        chunks: list[str] = []
        current = "<b>Current bot groups</b>"
        for heading, groups in sections:
            if not groups:
                continue
            heading_text = f"\n\n<b>{heading} ({len(groups)})</b>"
            if len(current) + len(heading_text) > 3800:
                chunks.append(current)
                current = "<b>Current bot groups (continued)</b>"
            current += heading_text
            for group in groups:
                line = "\n" + self._group_report_line(group)
                if len(current) + len(line) > 3800:
                    chunks.append(current)
                    current = "<b>Current bot groups (continued)</b>\n" + self._group_report_line(group)
                else:
                    current += line
        if not chunks and current == "<b>Current bot groups</b>":
            current += "\n\nNo group membership is currently confirmed."
        chunks.append(current)
        for chunk in chunks:
            self._safe_message(chat_id, chunk, html_mode=True)

    def _show_groups(self, owner_id: int) -> None:
        self._refresh_group_registry()
        self._maintain_group_access()
        current = self.group_registry.current_groups()
        uncertain = [group for group in self.group_registry.all_groups() if group.get("telegram_status") == "unknown"]
        self._send_long_html(
            owner_id,
            [
                ("Approved", [group for group in current if group.get("access_status") == "approved"]),
                ("Pending", [group for group in current if group.get("access_status") == "pending"]),
                (
                    "Blocked; leave will be retried",
                    [group for group in current if group.get("access_status") in BLOCKED_ACCESS_STATUSES],
                ),
                ("Unreviewed", [group for group in current if group.get("access_status") == "unreviewed"]),
                ("Membership could not be verified", uncertain),
            ],
        )

    def _show_pending_groups(self, owner_id: int) -> None:
        self._refresh_group_registry()
        self._maintain_group_access(flush_notifications=False)
        groups = self.group_registry.pending_groups()
        if not groups:
            self._safe_message(owner_id, "There are no pending groups.")
            return
        self._safe_message(owner_id, f"Pending groups: {len(groups)}")
        for group in groups[:50]:
            self._send_pending_card(owner_id, group, track_delivery=True)
        if len(groups) > 50:
            self._safe_message(owner_id, f"Showing the first 50 of {len(groups)} pending groups.")

    def _answer_callback(self, callback_id: Any, text: str, *, alert: bool = False) -> None:
        try:
            self.bot.answer_callback_query(callback_id, text=text, show_alert=alert)
        except Exception:
            self.log.info("cannot answer group approval callback", exc_info=True)

    def _handle_group_access_callback(self, call: Any) -> None:
        data = str(getattr(call, "data", "") or "")
        parts = data.split(":")
        callback_id = getattr(call, "id", None)
        if not self._approval_required:
            self._answer_callback(callback_id, "Group approval is disabled.", alert=True)
            return
        if len(parts) != 3 or parts[0] != "ga" or parts[1] not in {"a", "r"}:
            self._answer_callback(callback_id, "Invalid request.", alert=True)
            return
        try:
            user_id = int(call.from_user.id)
        except (AttributeError, TypeError, ValueError):
            self._answer_callback(callback_id, "Not authorized.", alert=True)
            return
        if not self.group_registry.is_owner(user_id):
            self._answer_callback(callback_id, "Not authorized.", alert=True)
            return
        decision = "approved" if parts[1] == "a" else "rejected"
        result, group = self.group_registry.resolve_request(parts[2], decision, user_id)
        if result == "not_found" or group is None:
            self._answer_callback(callback_id, "This request no longer exists.", alert=True)
            return
        if result == "already_resolved":
            current = str(group.get("access_status") or "resolved")
            self._answer_callback(callback_id, f"Already {current}.")
            return
        if decision == "approved":
            self._notify_group_approved(group)
            self._answer_callback(callback_id, "Group approved.")
        else:
            self._leave_blocked_group(group, notify=True)
            self._answer_callback(callback_id, "Group rejected.")
        message = getattr(call, "message", None)
        with suppress(Exception):
            self.bot.edit_message_reply_markup(message.chat.id, message.message_id, reply_markup=None)

    def _handle_my_chat_member(self, update: Any) -> None:
        chat = getattr(update, "chat", None)
        if getattr(chat, "type", "") not in {"group", "supergroup"}:
            return
        new_status = str(getattr(getattr(update, "new_chat_member", None), "status", "") or "unknown")
        old_status = str(getattr(getattr(update, "old_chat_member", None), "status", "") or "unknown")
        new_active = new_status.lower() in {"member", "restricted", "administrator", "creator"}
        old_active = old_status.lower() in {"member", "restricted", "administrator", "creator"}
        membership_started = new_active and not old_active
        allowed = self._record_group_access(
            chat,
            added_by=getattr(update, "from_user", None),
            membership_started=membership_started,
            telegram_status=new_status,
        )
        if allowed and membership_started:
            self._welcome_group(int(chat.id))

    def _handle_chat_migration(self, message: Any) -> None:
        old_chat_id: int | None = None
        new_chat_id: int | None = None
        if isinstance(getattr(message, "migrate_to_chat_id", None), int):
            old_chat_id = int(message.chat.id)
            new_chat_id = int(message.migrate_to_chat_id)
        elif isinstance(getattr(message, "migrate_from_chat_id", None), int):
            old_chat_id = int(message.migrate_from_chat_id)
            new_chat_id = int(message.chat.id)
        if old_chat_id is None or new_chat_id is None:
            return
        self.storage.migrate_chat_id(old_chat_id, new_chat_id)
        self.group_registry.migrate_chat_id(old_chat_id, new_chat_id)
        self.log.info("group migrated old_chat_id=%s new_chat_id=%s", old_chat_id, new_chat_id)

    def _set_commands(self) -> None:
        try:
            self.bot.set_my_commands(
                [
                    telebot.types.BotCommand("start", "Show instructions"),
                    telebot.types.BotCommand("help", "Show instructions"),
                    telebot.types.BotCommand("en", "Switch to English (admins)"),
                    telebot.types.BotCommand("ru", "Переключить на русский (админы)"),
                    telebot.types.BotCommand("settings", "Show group settings"),
                    telebot.types.BotCommand("delete_original", "Configure link deletion (admins)"),
                ]
            )
            owner_id = self.group_registry.owner_id()
            if owner_id is not None:
                self._set_owner_commands(owner_id)
        except Exception:
            self.log.exception("cannot set Telegram commands")

    def _handle_language_command(self, message: Any, requested: str) -> None:
        chat_id = int(message.chat.id)
        current = self.storage.chat_language(chat_id)
        if getattr(message.chat, "type", "") != "private" and not self._is_admin(chat_id, int(message.from_user.id)):
            self._safe_message(chat_id, tr(current, "language_admin_only"))
            return
        self.storage.set_chat_language(chat_id, requested)
        self._safe_message(chat_id, tr(requested, "language_changed"))

    def _register_handlers(self) -> None:
        bot = self.bot

        @bot.my_chat_member_handler(func=lambda _update: True)
        def membership_changed(update: Any) -> None:
            self._handle_my_chat_member(update)

        @bot.callback_query_handler(func=lambda call: str(getattr(call, "data", "") or "").startswith("ga:"))
        def group_access_callback(call: Any) -> None:
            self._handle_group_access_callback(call)

        @bot.message_reaction_handler(func=lambda _update: True)
        def retry_reaction(update: Any) -> None:
            self._handle_retry_reaction(update)

        @bot.message_handler(content_types=["new_chat_members"])
        def new_members(message: Any) -> None:
            if getattr(message.chat, "type", "") not in {"group", "supergroup"}:
                return
            members = getattr(message, "new_chat_members", None) or []
            if not any(int(getattr(member, "id", 0) or 0) == self.bot_id for member in members):
                return
            chat_id = int(message.chat.id)
            existing = self.group_registry.get_group(chat_id) or {}
            known_status = str(existing.get("telegram_status") or "member")
            if known_status not in ACTIVE_TELEGRAM_STATUSES:
                known_status = "member"
            if not self._record_group_access(
                message.chat,
                added_by=getattr(message, "from_user", None),
                membership_started=True,
                telegram_status=known_status,
            ):
                return
            self._welcome_group(chat_id, getattr(message, "message_thread_id", None))

        @bot.message_handler(content_types=["migrate_to_chat_id", "migrate_from_chat_id"])
        def chat_migration(message: Any) -> None:
            self._handle_chat_migration(message)

        @bot.message_handler(commands=["start", "help"])
        def start_help(message: Any) -> None:
            chat_type = getattr(message.chat, "type", "")
            chat_id = int(message.chat.id)
            private = chat_type == "private"
            if not private and chat_type not in {"group", "supergroup"}:
                return
            if private:
                self._maybe_bind_owner(message.from_user)
            elif not self._record_group_access(message.chat):
                return
            self._safe_message(
                chat_id,
                self._help(chat_id, private=private),
                getattr(message, "message_thread_id", None),
                html_mode=True,
            )
            if private:
                self.storage.mark_welcomed("private", int(message.from_user.id))

        @bot.message_handler(commands=["en", "ru"])
        def language(message: Any) -> None:
            if getattr(message.chat, "type", "") in {"group", "supergroup"} and not self._record_group_access(
                message.chat
            ):
                return
            command = (message.text or "").split(maxsplit=1)[0]
            requested = command.split("@", 1)[0].lstrip("/").lower()
            self._handle_language_command(message, requested)

        @bot.message_handler(commands=["settings"])
        def group_settings(message: Any) -> None:
            chat_id = int(message.chat.id)
            language_code = self.storage.chat_language(chat_id)
            if getattr(message.chat, "type", "") not in {"group", "supergroup"}:
                return
            if not self._record_group_access(message.chat):
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
            if not self._record_group_access(message.chat):
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

        @bot.message_handler(commands=["groups", "pending_groups"])
        def owner_groups(message: Any) -> None:
            if getattr(message.chat, "type", "") != "private":
                return
            user_id = int(message.from_user.id)
            if not self._maybe_bind_owner(message.from_user) or not self.group_registry.is_owner(user_id):
                self._safe_message(
                    int(message.chat.id), tr(self.storage.chat_language(int(message.chat.id)), "private_hint")
                )
                return
            command = (message.text or "").split(maxsplit=1)[0].split("@", 1)[0].lower()
            if command == "/groups":
                self._show_groups(user_id)
            else:
                self._show_pending_groups(user_id)

        @bot.message_handler(func=lambda item: getattr(item.chat, "type", "") == "private", content_types=["text"])
        def private_text(message: Any) -> None:
            chat_id = int(message.chat.id)
            user_id = int(message.from_user.id)
            self._maybe_bind_owner(message.from_user)
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
        job: Job | None = None
        try:
            if getattr(message.chat, "type", "") not in {"group", "supergroup"}:
                return
            if not self._record_group_access(message.chat):
                return
            group = self.group_registry.get_group(int(message.chat.id)) or {}
            if group.get("resolution") in {"owner_approved", "added_by_owner"}:
                self._welcome_group(int(message.chat.id), getattr(message, "message_thread_id", None))
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
            self._set_status_reaction(job, "👀")
            flight = self.coordinator.submit(job)
            if flight is None:
                self.log.info("job joined URL flight job_id=%s url=%s", job.job_id, safe_url_for_log(url))
                return
            try:
                self.queue.put_nowait(flight)
                self.log.info("job queued job_id=%s chat_id=%s url=%s", job.job_id, chat_id, safe_url_for_log(url))
            except queue.Full:
                self._clear_status_many(self.coordinator.abort(flight))
                self.log.warning("queue full job_id=%s chat_id=%s", job.job_id, chat_id)
                self._operator_alert("The download queue is full. Check bot.log.")
        except UnsafeUrlError:
            self.log.warning("unsafe URL rejected chat_id=%s", getattr(message.chat, "id", None), exc_info=True)
        except Exception:
            if job is not None:
                self._clear_status_reaction(job)
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
