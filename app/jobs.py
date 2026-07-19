from __future__ import annotations

import threading
from dataclasses import dataclass, field


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


@dataclass
class Flight:
    url_keys: set[str]
    jobs: list[Job] = field(default_factory=list)
    cursor: int = 0
    media_key: str | None = None


class FlightCoordinator:
    """Coalesce equal URLs first and equal extractor media IDs after metadata."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_url: dict[str, Flight] = {}
        self._by_media: dict[str, Flight] = {}

    def submit(self, job: Job) -> Flight | None:
        with self._lock:
            existing = self._by_url.get(job.url_key)
            if existing is not None:
                existing.jobs.append(job)
                return None
            flight = Flight(url_keys={job.url_key}, jobs=[job])
            self._by_url[job.url_key] = flight
            return flight

    def promote(self, flight: Flight, media_key: str) -> bool:
        with self._lock:
            existing = self._by_media.get(media_key)
            if existing is flight:
                return True
            if existing is not None:
                existing.jobs.extend(flight.jobs[flight.cursor :])
                existing.url_keys.update(flight.url_keys)
                for url_key in flight.url_keys:
                    self._by_url[url_key] = existing
                flight.cursor = len(flight.jobs)
                return False
            flight.media_key = media_key
            self._by_media[media_key] = flight
            return True

    def pending(self, flight: Flight) -> list[Job]:
        with self._lock:
            jobs = list(flight.jobs[flight.cursor :])
            flight.cursor = len(flight.jobs)
            return jobs

    def finish_if_idle(self, flight: Flight) -> bool:
        with self._lock:
            if flight.cursor < len(flight.jobs):
                return False
            for url_key in flight.url_keys:
                if self._by_url.get(url_key) is flight:
                    self._by_url.pop(url_key, None)
            if flight.media_key and self._by_media.get(flight.media_key) is flight:
                self._by_media.pop(flight.media_key, None)
            return True

    def abort(self, flight: Flight) -> list[Job]:
        with self._lock:
            jobs = list(flight.jobs[flight.cursor :])
            flight.cursor = len(flight.jobs)
            self.finish_if_idle(flight)
            return jobs
