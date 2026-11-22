from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

MessageRef = tuple[int, int]


class MessageJanitor:
    """Delete short-lived bot messages once they have served their purpose.

    Deletion is best effort: a message may already be gone, or the bot may lack
    the right to delete it, and neither case may affect the caller.
    """

    def __init__(
        self,
        delete_message: Callable[[int, int], Any],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._delete_message = delete_message
        self._clock = clock
        self._lock = threading.Lock()
        self._due: dict[MessageRef, float] = {}
        self.log = logging.getLogger("link_downloader_bot.janitor")

    def schedule(self, chat_id: int, message_id: int, delay_seconds: float) -> None:
        """Delete the message after the delay; a later call for the same message replaces the earlier one."""
        with self._lock:
            self._due[(int(chat_id), int(message_id))] = self._clock() + max(0.0, delay_seconds)

    def delete_now(self, chat_id: int, message_id: int) -> None:
        with self._lock:
            self._due.pop((int(chat_id), int(message_id)), None)
        self._delete(int(chat_id), int(message_id))

    def run_due(self) -> None:
        now = self._clock()
        with self._lock:
            due = [ref for ref, deadline in self._due.items() if deadline <= now]
            for ref in due:
                del self._due[ref]
        for chat_id, message_id in due:
            self._delete(chat_id, message_id)

    def flush(self) -> None:
        """Delete everything still scheduled; used on shutdown so nothing is left behind."""
        with self._lock:
            pending = list(self._due)
            self._due.clear()
        for chat_id, message_id in pending:
            self._delete(chat_id, message_id)

    def _delete(self, chat_id: int, message_id: int) -> None:
        try:
            self._delete_message(chat_id, message_id)
        except Exception as exc:
            self.log.info(
                "message cleanup skipped chat_id=%s message_id=%s error=%s",
                chat_id,
                message_id,
                type(exc).__name__,
            )
