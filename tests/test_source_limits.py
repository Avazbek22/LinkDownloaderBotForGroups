from __future__ import annotations

from app.source_limits import SourceCooldowns, source_platform


def test_source_platform_recognizes_only_protected_upstreams() -> None:
    assert source_platform("https://www.instagram.com/reel/example/") == "instagram"
    assert source_platform("https://youtu.be/example") == "youtube"
    assert source_platform("https://example.com/video") is None


def test_cooldown_blocks_access_and_allows_only_one_recovery_probe() -> None:
    now = [100.0]
    cooldowns = SourceCooldowns(60, 300, clock=lambda: now[0])

    first = cooldowns.acquire("instagram")
    assert first.allowed and not first.probe

    opened = cooldowns.limited("instagram", first)
    assert opened.extended
    assert opened.failure_count == 1
    assert opened.retry_after == 60
    assert not cooldowns.acquire("instagram").allowed
    assert cooldowns.acquire("youtube").allowed

    now[0] += 60
    probe = cooldowns.acquire("instagram")
    assert probe.allowed and probe.probe
    assert not cooldowns.acquire("instagram").allowed

    cooldowns.complete(probe)
    recovered = cooldowns.acquire("instagram")
    assert recovered.allowed and not recovered.probe


def test_repeated_limit_uses_bounded_backoff_without_concurrent_extension() -> None:
    now = [10.0]
    cooldowns = SourceCooldowns(30, 90, clock=lambda: now[0])

    first = cooldowns.acquire("youtube")
    assert cooldowns.limited("youtube", first).retry_after == 30
    duplicate = cooldowns.limited("youtube", first)
    assert not duplicate.extended
    assert duplicate.failure_count == 1

    now[0] += 30
    probe = cooldowns.acquire("youtube")
    second = cooldowns.limited("youtube", probe)
    assert second.extended
    assert second.failure_count == 2
    assert second.retry_after == 60

    now[0] += 60
    probe = cooldowns.acquire("youtube")
    third = cooldowns.limited("youtube", probe)
    assert third.failure_count == 3
    assert third.retry_after == 90


def test_stale_probe_cannot_clear_a_newer_cooldown_generation() -> None:
    now = [0.0]
    cooldowns = SourceCooldowns(10, 40, clock=lambda: now[0])
    cooldowns.limited("instagram")
    now[0] = 10
    stale_probe = cooldowns.acquire("instagram")

    cooldowns.limited("instagram")
    now[0] = 30
    current_probe = cooldowns.acquire("instagram")
    assert current_probe.allowed and current_probe.probe

    cooldowns.complete(stale_probe)
    assert not cooldowns.acquire("instagram").allowed
    cooldowns.complete(current_probe)
    assert cooldowns.acquire("instagram").allowed
