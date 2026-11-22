from __future__ import annotations

import os
import time

from app import jobs
from app.jobs import FlightCoordinator, Job, MediaKind
from app.media_cache import DiskMediaCache


def _job(job_id: str, url_key: str = "url", *, media_kind: MediaKind = "video") -> Job:
    return Job(
        job_id,
        -1,
        None,
        1,
        2,
        "https://example.com/v",
        url_key,
        "User",
        True,
        media_kind=media_kind,
    )


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


def test_video_and_audio_requests_never_share_a_flight() -> None:
    coordinator = FlightCoordinator()

    video = coordinator.submit(_job("video", media_kind="video"))
    audio = coordinator.submit(_job("audio", media_kind="audio"))

    assert video is not None
    assert audio is not None
    assert coordinator.promote(video, "youtube:id")
    assert coordinator.promote(audio, "youtube:id")
    assert [job.job_id for job in coordinator.pending(video)] == ["video"]
    assert [job.job_id for job in coordinator.pending(audio)] == ["audio"]
    assert coordinator.finish_if_idle(video)
    assert coordinator.finish_if_idle(audio)


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


def test_disk_cache_never_removes_unowned_files(tmp_path) -> None:
    cache = DiskMediaCache(tmp_path, max_files=1, ttl_seconds=30)
    unrelated = tmp_path / "cookies.txt"
    unrelated.write_text("keep", encoding="utf-8")
    os.utime(unrelated, (time.time() - 300, time.time() - 300))
    owned = tmp_path / f"{cache.prefix('video')}.mp4"
    owned.write_bytes(b"video")

    cache.maintain()

    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert owned.exists()


def _sender_job(job_id: str, message_id: int, user_id: int, url_key: str = "url") -> Job:
    return Job(job_id, -1, None, message_id, user_id, "https://example.com/v", url_key, "User", True)


def test_only_the_sender_can_cancel_a_live_job_and_only_once() -> None:
    coordinator = FlightCoordinator()
    job = _sender_job("a", message_id=10, user_id=7)
    assert coordinator.submit(job) is not None

    assert coordinator.cancel(-1, 10, 8) is None
    assert coordinator.cancel(-1, 11, 7) is None
    assert coordinator.cancel(-1, 10, 7) == job
    assert coordinator.cancel(-1, 10, 7) is None
    assert coordinator.is_cancelled(job)


def test_job_with_a_result_can_no_longer_be_cancelled() -> None:
    coordinator = FlightCoordinator()
    delivered = _sender_job("a", message_id=10, user_id=7)
    waiting = _sender_job("b", message_id=11, user_id=7)
    flight = coordinator.submit(delivered)
    assert flight is not None
    assert coordinator.submit(waiting) is None

    assert coordinator.settle(delivered) is False
    assert coordinator.cancel(-1, 10, 7) is None
    assert coordinator.cancel(-1, 11, 7) == waiting
    assert coordinator.settle(waiting) is True


def test_finished_flight_ignores_late_cancels_but_remembers_earlier_ones() -> None:
    coordinator = FlightCoordinator()
    cancelled = _sender_job("a", message_id=10, user_id=7)
    flight = coordinator.submit(cancelled)
    assert flight is not None
    assert coordinator.cancel(-1, 10, 7) == cancelled

    coordinator.abort(flight)

    assert coordinator.is_cancelled(cancelled)
    assert coordinator.settle(cancelled) is True
    assert coordinator.submit(_sender_job("b", message_id=12, user_id=7)) is not None
    assert coordinator.cancel(-1, 10, 7) is None


def test_flight_is_wanted_while_any_batch_or_joined_job_was_not_cancelled() -> None:
    coordinator = FlightCoordinator()
    first = _sender_job("a", message_id=10, user_id=7)
    flight = coordinator.submit(first)
    assert flight is not None
    batch = coordinator.pending(flight)
    coordinator.cancel(-1, 10, 7)
    assert not coordinator.wanted(flight, batch)

    joined = _sender_job("b", message_id=11, user_id=8)
    assert coordinator.submit(joined) is None
    assert coordinator.wanted(flight, batch)
    coordinator.cancel(-1, 11, 8)
    assert not coordinator.wanted(flight, batch)


def test_cancel_marks_are_bounded(monkeypatch) -> None:
    monkeypatch.setattr(jobs, "CANCELLED_JOBS_MAX_ITEMS", 2)
    coordinator = FlightCoordinator()
    cancelled = []
    for index in range(3):
        job = _sender_job(str(index), message_id=index, user_id=7, url_key=f"url-{index}")
        assert coordinator.submit(job) is not None
        assert coordinator.cancel(-1, index, 7) == job
        cancelled.append(job)

    assert [coordinator.is_cancelled(job) for job in cancelled] == [False, True, True]
