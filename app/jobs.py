from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Literal

MediaKind = Literal["video", "audio"]


@dataclass(frozen=True)
class Job:
    job_id: str
    chat_id: int
    message_thread_id: int | None
    original_message_id: int
    user_id: int
    url: str
    url_key: str
    sender_name: str
    delete_original: bool
    runtime_revision: int = 0
    media_kind: MediaKind = "video"


@dataclass
class Flight:
    url_keys: set[str]
    media_kind: MediaKind
    jobs: list[Job] = field(default_factory=list)
    cursor: int = 0
    media_key: str | None = None


class FlightCoordinator:
    """Coalesce equal URLs and media IDs without mixing delivery formats."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_url: dict[tuple[MediaKind, str], Flight] = {}
        self._by_media: dict[tuple[MediaKind, str], Flight] = {}

    def submit(self, job: Job) -> Flight | None:
        with self._lock:
            request_key = job.media_kind, job.url_key
            existing = self._by_url.get(request_key)
            if existing is not None:
                existing.jobs.append(job)
                return None
            flight = Flight(url_keys={job.url_key}, media_kind=job.media_kind, jobs=[job])
            self._by_url[request_key] = flight
            return flight

    def promote(self, flight: Flight, media_key: str) -> bool:
        with self._lock:
            request_key = flight.media_kind, media_key
            existing = self._by_media.get(request_key)
            if existing is flight:
                return True
            if existing is not None:
                existing.jobs.extend(flight.jobs[flight.cursor :])
                existing.url_keys.update(flight.url_keys)
                for url_key in flight.url_keys:
                    self._by_url[(flight.media_kind, url_key)] = existing
                flight.cursor = len(flight.jobs)
                return False
            flight.media_key = media_key
            self._by_media[request_key] = flight
            return True

    def pending(self, flight: Flight) -> list[Job]:
        with self._lock:
            jobs = list(flight.jobs[flight.cursor :])
            flight.cursor = len(flight.jobs)
            return jobs

    def jobs_for_chat(self, chat_id: int) -> list[Job]:
        """Return live queued or in-flight jobs for a chat without mutating flights."""
        with self._lock:
            flights: dict[int, Flight] = {}
            for flight in (*self._by_url.values(), *self._by_media.values()):
                flights[id(flight)] = flight
            return [job for flight in flights.values() for job in flight.jobs if job.chat_id == int(chat_id)]

    def finish_if_idle(self, flight: Flight) -> bool:
        with self._lock:
            if flight.cursor < len(flight.jobs):
                return False
            for url_key in flight.url_keys:
                request_key = flight.media_kind, url_key
                if self._by_url.get(request_key) is flight:
                    self._by_url.pop(request_key, None)
            media_request_key = (flight.media_kind, flight.media_key) if flight.media_key else None
            if media_request_key and self._by_media.get(media_request_key) is flight:
                self._by_media.pop(media_request_key, None)
            return True

    def abort(self, flight: Flight) -> list[Job]:
        with self._lock:
            jobs = list(flight.jobs[flight.cursor :])
            flight.cursor = len(flight.jobs)
            self.finish_if_idle(flight)
            return jobs
