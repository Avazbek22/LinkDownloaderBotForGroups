from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from app.url_utils import is_instagram_url, is_youtube_url

SourcePlatform = Literal["instagram", "youtube"]


def source_platform(url: str) -> SourcePlatform | None:
    if is_instagram_url(url):
        return "instagram"
    if is_youtube_url(url):
        return "youtube"
    return None


@dataclass(frozen=True)
class SourceAccess:
    source: SourcePlatform
    allowed: bool
    probe: bool = False
    retry_after: float = 0.0
    generation: int = 0


@dataclass(frozen=True)
class CooldownUpdate:
    source: SourcePlatform
    failure_count: int
    retry_after: float
    extended: bool


@dataclass
class _CooldownState:
    failure_count: int
    blocked_until: float
    probe_in_flight: bool = False
    generation: int = 1


class SourceCooldowns:
    """Thread-safe, per-platform circuit breaker for upstream access limits."""

    def __init__(
        self,
        initial_seconds: int,
        max_seconds: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if initial_seconds <= 0:
            raise ValueError("initial_seconds must be positive")
        if max_seconds < initial_seconds:
            raise ValueError("max_seconds must not be smaller than initial_seconds")
        self.initial_seconds = initial_seconds
        self.max_seconds = max_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._states: dict[SourcePlatform, _CooldownState] = {}

    def acquire(self, source: SourcePlatform) -> SourceAccess:
        """Allow normal access, reject a live cooldown, or grant one recovery probe."""
        now = self._clock()
        with self._lock:
            state = self._states.get(source)
            if state is None:
                return SourceAccess(source=source, allowed=True)
            retry_after = max(0.0, state.blocked_until - now)
            if retry_after > 0 or state.probe_in_flight:
                return SourceAccess(
                    source=source,
                    allowed=False,
                    retry_after=retry_after,
                    generation=state.generation,
                )
            state.probe_in_flight = True
            return SourceAccess(source=source, allowed=True, probe=True, generation=state.generation)

    def complete(self, access: SourceAccess | None) -> None:
        """Close a successful recovery probe without disturbing a newer cooldown."""
        if access is None or not access.probe:
            return
        with self._lock:
            state = self._states.get(access.source)
            if state is not None and state.probe_in_flight and state.generation == access.generation:
                self._states.pop(access.source, None)

    def limited(self, source: SourcePlatform, access: SourceAccess | None = None) -> CooldownUpdate:
        """Open or extend a cooldown after an explicit upstream rate-limit response."""
        now = self._clock()
        with self._lock:
            state = self._states.get(source)
            matching_probe = bool(
                state is not None
                and state.probe_in_flight
                and access
                and access.source == source
                and access.probe
                and access.generation == state.generation
            )
            if state is not None and state.blocked_until > now and not matching_probe:
                return CooldownUpdate(
                    source=source,
                    failure_count=state.failure_count,
                    retry_after=state.blocked_until - now,
                    extended=False,
                )

            failure_count = state.failure_count + 1 if state is not None else 1
            multiplier = 2 ** min(failure_count - 1, self.max_seconds.bit_length())
            duration = min(self.initial_seconds * multiplier, self.max_seconds)
            self._states[source] = _CooldownState(
                failure_count=failure_count,
                blocked_until=now + duration,
                generation=state.generation + 1 if state is not None else 1,
            )
            return CooldownUpdate(
                source=source,
                failure_count=failure_count,
                retry_after=float(duration),
                extended=True,
            )
