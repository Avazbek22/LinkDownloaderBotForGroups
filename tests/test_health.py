from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pytest

from app import healthcheck
from main import BotApplication


class FakeBot:
    def __init__(self) -> None:
        self.fail = False

    def get_updates(self, *args: Any, **kwargs: Any) -> list[object]:
        del args, kwargs
        if self.fail:
            raise ConnectionError("Conflict: terminated by other getUpdates request")
        return []


def application_with(marker: Path) -> tuple[BotApplication, FakeBot]:
    application = BotApplication.__new__(BotApplication)
    fake = FakeBot()
    application.bot = fake  # type: ignore[assignment]
    application.stop_event = threading.Event()
    application.health_marker = marker
    application._mark_healthy_after_each_poll()
    return application, fake


def test_successful_poll_refreshes_health_marker(tmp_path: Path) -> None:
    marker = tmp_path / "health"
    _, fake = application_with(marker)
    assert fake.get_updates(offset=1) == []
    assert marker.is_file()


def test_failed_poll_does_not_refresh_health_marker(tmp_path: Path) -> None:
    marker = tmp_path / "health"
    _, fake = application_with(marker)
    fake.fail = True
    with pytest.raises(ConnectionError):
        fake.get_updates(offset=1)
    assert not marker.exists()


def test_poll_after_stop_does_not_mark_healthy(tmp_path: Path) -> None:
    marker = tmp_path / "health"
    application, fake = application_with(marker)
    application.stop_event.set()
    fake.get_updates(offset=1)
    assert not marker.exists()


def test_healthcheck_requires_a_fresh_marker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    marker = tmp_path / "linkdownloaderbot.healthy"
    monkeypatch.setenv("HEALTH_MARKER", str(marker))
    assert healthcheck.main() == 1
    marker.touch()
    assert healthcheck.main() == 0
    stale = marker.stat().st_mtime - healthcheck.MAX_AGE_SECONDS - 1
    os.utime(marker, (stale, stale))
    assert healthcheck.main() == 1
