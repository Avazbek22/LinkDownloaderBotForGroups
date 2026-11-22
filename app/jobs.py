from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Literal

MediaKind = Literal["video", "audio"]
CANCELLED_JOBS_MAX_ITEMS = 1_000


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
    reply_to_message_id: int | None = None


@dataclass
class Flight:
    url_keys: set[str]
    media_kind: MediaKind
    jobs: list[Job] = field(default_factory=list)
    cursor: int = 0
    media_key: str | None = None
    # Jobs that already have a result; their senders can no longer cancel them.
    settled_job_ids: set[str] = field(default_factory=set)


class FlightCoordinator:
    """Coalesce equal URLs and media IDs without mixing delivery formats."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_url: dict[tuple[MediaKind, str], Flight] = {}
        self._by_media: dict[tuple[MediaKind, str], Flight] = {}
        # Outlives the flight: outcomes are often reported after the flight was aborted.
        self._cancelled: OrderedDict[str, None] = OrderedDict()

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

    def _live_flights(self) -> list[Flight]:
        flights: dict[int, Flight] = {}
        for flight in (*self._by_url.values(), *self._by_media.values()):
            flights[id(flight)] = flight
        return list(flights.values())

    def jobs_for_chat(self, chat_id: int) -> list[Job]:
        """Return live queued or in-flight jobs for a chat without mutating flights."""
        with self._lock:
            return [job for flight in self._live_flights() for job in flight.jobs if job.chat_id == int(chat_id)]

    def cancel(self, chat_id: int, message_id: int, user_id: int) -> Job | None:
        """Cancel the sender's own job for a message while its result is still pending."""
        with self._lock:
            for flight in self._live_flights():
                for job in flight.jobs:
                    if (job.chat_id, job.original_message_id, job.user_id) != (chat_id, message_id, user_id):
                        continue
                    if job.job_id in flight.settled_job_ids or job.job_id in self._cancelled:
                        continue
                    self._cancelled[job.job_id] = None
                    while len(self._cancelled) > CANCELLED_JOBS_MAX_ITEMS:
                        self._cancelled.popitem(last=False)
                    return job
            return None

    def is_cancelled(self, job: Job) -> bool:
        with self._lock:
            return job.job_id in self._cancelled

    def settle(self, job: Job) -> bool:
        """Close the cancel window for a job that has its result; return whether it was cancelled first."""
        with self._lock:
            for flight in self._live_flights():
                if any(item.job_id == job.job_id for item in flight.jobs):
                    flight.settled_job_ids.add(job.job_id)
                    break
            return job.job_id in self._cancelled

    def wanted(self, flight: Flight, jobs: list[Job]) -> bool:
        """Return whether any of the jobs, or a job that joined the flight since, was not cancelled."""
        with self._lock:
            return any(job.job_id not in self._cancelled for job in (*jobs, *flight.jobs[flight.cursor :]))

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
