from __future__ import annotations

import os
import time

from app.jobs import FlightCoordinator, Job
from app.media_cache import DiskMediaCache


def _job(job_id: str, url_key: str = "url") -> Job:
    return Job(job_id, -1, None, 1, 2, "https://example.com/v", url_key, "User", True)


def test_coalesces_same_url_and_media() -> None:
    coordinator = FlightCoordinator()
    first = coordinator.submit(_job("a", "url-a"))
    assert first is not None
    assert coordinator.submit(_job("b", "url-a")) is None
    second = coordinator.submit(_job("c", "url-c"))
    assert second is not None
    assert coordinator.promote(first, "youtube:id")
    assert not coordinator.promote(second, "youtube:id")
    assert [job.job_id for job in coordinator.pending(first)] == ["a", "b", "c"]
    assert coordinator.finish_if_idle(first)


def test_new_job_prevents_flight_from_finishing() -> None:
    coordinator = FlightCoordinator()
    flight = coordinator.submit(_job("a"))
    assert flight is not None
    assert [job.job_id for job in coordinator.pending(flight)] == ["a"]
    assert coordinator.submit(_job("b")) is None
    assert not coordinator.finish_if_idle(flight)
    assert [job.job_id for job in coordinator.pending(flight)] == ["b"]
    assert coordinator.finish_if_idle(flight)


def test_disk_cache_ttl_and_lru(tmp_path) -> None:
    cache = DiskMediaCache(tmp_path, max_files=2, ttl_seconds=30)
    paths = []
    for index, key in enumerate(("a", "b", "c")):
        path = tmp_path / f"{cache.prefix(key)}.mp4"
        path.write_bytes(b"video")
        os.utime(path, (time.time() + index, time.time() + index))
        paths.append(path)
    cache.maintain()
    assert not paths[0].exists()
    assert paths[1].exists() and paths[2].exists()

    old = paths[1]
    os.utime(old, (time.time() - 60, time.time() - 60))
    assert cache.get("b") is None
    assert not old.exists()
