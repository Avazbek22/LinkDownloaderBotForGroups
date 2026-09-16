from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.group_registry import GroupRegistry


def _user(user_id: int, username: str) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, username=username, first_name="Test", last_name="Owner")


def test_owner_binding_uses_username_once_then_stable_numeric_id(tmp_path) -> None:
    registry = GroupRegistry(tmp_path, "@Owner_Name", "approval")

    assert registry.bind_owner(10, "someone_else") == "username_mismatch"
    assert registry.bind_owner(42, "OWNER_NAME") == "claimed"
    assert registry.owner_id() == 42
    assert registry.bind_owner(42, "renamed_owner") == "owner"
    assert registry.bind_owner(99, "owner_name") == "different_owner"
    assert registry.owner_id() == 42

    restarted = GroupRegistry(tmp_path, "owner_name", "approval")
    assert restarted.owner_id() == 42
    assert restarted.bind_owner(42, None) == "owner"


def test_existing_registry_is_migrated_to_active_runtime_mode(tmp_path) -> None:
    legacy = {
        "version": 1,
        "owner": {"configured_username": "owner_name", "user_id": 42, "bound_at": None},
        "policy": {"mode": "approval"},
        "groups": {
            "-1001": {
                "chat_id": -1001,
                "title": "Existing",
                "type": "supergroup",
                "telegram_status": "member",
                "access_status": "approved",
                "first_seen_at": "2026-01-01T00:00:00+00:00",
            }
        },
    }
    (tmp_path / "groups.json").write_text(json.dumps(legacy), encoding="utf-8")

    registry = GroupRegistry(tmp_path, "owner_name", "approval")
    group = registry.get_group(-1001)

    assert registry.snapshot()["version"] == 2
    assert GroupRegistry.runtime_mode(group) == "active"
    assert GroupRegistry.runtime_revision(group) == 0
    assert group["runtime_history"] == []
    assert registry.access_allowed(-1001, True)


def test_unapproved_group_is_pending_and_owner_addition_is_approved(tmp_path) -> None:
    registry = GroupRegistry(tmp_path, "owner_name", "approval")
    assert registry.bind_owner(42, "owner_name") == "claimed"

    pending = registry.record_presence(
        -1001,
        title="Pending",
        chat_type="supergroup",
        telegram_status="member",
        added_by=_user(7, "member"),
        approval_required=True,
        membership_started=True,
    )
    approved = registry.record_presence(
        -1002,
        title="Owned",
        chat_type="supergroup",
        telegram_status="administrator",
        added_by=_user(42, "renamed_owner"),
        approval_required=True,
        membership_started=True,
    )

    assert pending["access_status"] == "pending"
    assert pending["request_id"]
    assert not registry.access_allowed(-1001, True)
    assert approved["access_status"] == "approved"
    assert registry.access_allowed(-1002, True)


def test_owner_binding_later_approves_groups_the_owner_added(tmp_path) -> None:
    registry = GroupRegistry(tmp_path, "owner_name", "approval")
    group = registry.record_presence(
        -1001,
        title="Before binding",
        chat_type="supergroup",
        telegram_status="member",
        added_by=_user(42, "owner_name"),
        approval_required=True,
        membership_started=True,
    )
    assert group["access_status"] == "pending"

    assert registry.bind_owner(42, "owner_name") == "claimed"
    approved = registry.approve_pending_added_by(42)

    assert [item["chat_id"] for item in approved] == [-1001]
    assert registry.get_group(-1001)["access_status"] == "approved"


def test_notification_claim_is_atomic_and_failed_delivery_becomes_retryable(tmp_path) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    registry = GroupRegistry(tmp_path, "owner_name", "approval")
    group = registry.record_presence(
        -1001,
        title="Pending",
        chat_type="supergroup",
        telegram_status="member",
        approval_required=True,
        now=now,
    )

    first = registry.claim_pending_notifications(now=now)
    second = registry.claim_pending_notifications(now=now)
    assert [item["chat_id"] for item in first] == [-1001]
    assert second == []

    assert registry.mark_notification(group["request_id"], False, now=now)
    assert registry.claim_pending_notifications(now=now + timedelta(seconds=299)) == []
    assert [item["chat_id"] for item in registry.claim_pending_notifications(now=now + timedelta(seconds=300))] == [
        -1001
    ]


def test_group_notice_is_sent_at_most_once_but_a_failed_attempt_can_retry(tmp_path) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    registry = GroupRegistry(tmp_path, "owner_name", "approval")
    registry.record_presence(
        -1001,
        title="Pending",
        chat_type="supergroup",
        telegram_status="member",
        approval_required=True,
        now=now,
    )

    assert registry.claim_group_notice(-1001, now=now)
    assert not registry.claim_group_notice(-1001, now=now)
    registry.mark_group_notice(-1001, False, now=now)
    assert not registry.claim_group_notice(-1001, now=now + timedelta(seconds=299))
    assert registry.claim_group_notice(-1001, now=now + timedelta(seconds=300))
    registry.mark_group_notice(-1001, True, now=now + timedelta(seconds=300))
    assert not registry.claim_group_notice(-1001, now=now + timedelta(days=1))


def test_resolution_is_idempotent_and_does_not_allow_decision_reversal(tmp_path) -> None:
    registry = GroupRegistry(tmp_path, "owner_name", "approval")
    group = registry.record_presence(
        -1001,
        title="Pending",
        chat_type="supergroup",
        telegram_status="member",
        approval_required=True,
    )

    result, resolved = registry.resolve_request(group["request_id"], "approved", 42)
    assert result == "resolved"
    assert resolved["access_status"] == "approved"

    repeated, unchanged = registry.resolve_request(group["request_id"], "rejected", 42)
    assert repeated == "already_resolved"
    assert unchanged["access_status"] == "approved"


def test_resolution_preserves_owner_card_for_durable_status_updates(tmp_path) -> None:
    registry = GroupRegistry(tmp_path, "owner_name", "approval")
    group = registry.record_presence(
        -1001,
        title="Pending",
        chat_type="supergroup",
        telegram_status="member",
        approval_required=True,
    )
    request_id = group["request_id"]
    assert registry.mark_notification(
        request_id,
        True,
        owner_chat_id=42,
        message_id=9,
        base_text="original card",
    )

    registry.resolve_request(request_id, "rejected", 42)

    notification = registry.get_group(-1001)["owner_notification"]
    assert notification["chat_id"] == 42
    assert notification["message_id"] == 9
    assert notification["base_text"] == "original card"
    assert registry.mark_owner_card_update(request_id, "rejected:leaving", True)
    assert registry.get_group(-1001)["owner_notification"]["decision_update"]["status"] == "sent"


def test_remembering_another_copy_of_a_resolved_card_makes_it_updateable(tmp_path) -> None:
    registry = GroupRegistry(tmp_path, "owner_name", "approval")
    group = registry.record_presence(
        -1001,
        title="Pending",
        chat_type="supergroup",
        telegram_status="member",
        approval_required=True,
    )
    request_id = group["request_id"]
    registry.resolve_request(request_id, "approved", 42)
    registry.mark_owner_card_update(request_id, "approved", True)

    assert registry.remember_owner_card(request_id, 42, 99, "duplicate card")

    notification = registry.get_group(-1001)["owner_notification"]
    assert notification["message_id"] == 99
    assert notification["base_text"] == "duplicate card"
    assert "decision_update" not in notification


def test_pending_expiry_survives_restart_and_approved_groups_survive_readd(tmp_path) -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    registry = GroupRegistry(tmp_path, "owner_name", "approval")
    registry.record_presence(
        -1001,
        title="Pending",
        chat_type="supergroup",
        telegram_status="member",
        approval_required=True,
        now=started,
    )
    approved = registry.record_presence(
        -1002,
        title="Approved",
        chat_type="supergroup",
        telegram_status="member",
        approval_required=True,
        now=started,
    )
    registry.resolve_request(approved["request_id"], "approved", 42, now=started)

    restarted = GroupRegistry(tmp_path, "owner_name", "approval")
    assert restarted.expire_pending(168, now=started + timedelta(hours=167, minutes=59)) == []
    expired = restarted.expire_pending(168, now=started + timedelta(hours=168))
    assert [item["chat_id"] for item in expired] == [-1001]
    assert restarted.get_group(-1001)["access_status"] == "expired"

    restarted.record_presence(
        -1002,
        title="Approved",
        chat_type="supergroup",
        telegram_status="left",
        approval_required=True,
    )
    readded = restarted.record_presence(
        -1002,
        title="Approved",
        chat_type="supergroup",
        telegram_status="member",
        approval_required=True,
        membership_started=True,
    )
    assert readded["access_status"] == "approved"


def test_soft_pause_is_persistent_revisioned_and_reversible(tmp_path) -> None:
    registry = GroupRegistry(tmp_path, "owner_name", "approval")
    group = registry.record_presence(
        -1001,
        title="Approved",
        chat_type="supergroup",
        telegram_status="member",
        approval_required=True,
    )
    registry.resolve_request(group["request_id"], "approved", 42)

    result, paused = registry.set_runtime_mode(-1001, "soft", 42, expected_revision=0)

    assert result == "changed"
    assert GroupRegistry.runtime_mode(paused) == "soft"
    assert GroupRegistry.runtime_revision(paused) == 1
    assert not registry.access_allowed(-1001, True)
    stale, _group = registry.set_runtime_mode(-1001, "active", 42, expected_revision=0)
    assert stale == "stale"

    restarted = GroupRegistry(tmp_path, "owner_name", "approval")
    assert GroupRegistry.runtime_mode(restarted.get_group(-1001)) == "soft"
    restarted.record_presence(
        -1001,
        title="Approved",
        chat_type="supergroup",
        telegram_status="left",
        approval_required=True,
    )
    readded = restarted.record_presence(
        -1001,
        title="Approved",
        chat_type="supergroup",
        telegram_status="member",
        added_by=_user(7, "member"),
        approval_required=True,
        membership_started=True,
    )
    assert GroupRegistry.runtime_mode(readded) == "soft"
    assert not restarted.access_allowed(-1001, True)
    resumed, active = restarted.set_runtime_mode(-1001, "active", 42, expected_revision=1)
    assert resumed == "changed"
    assert GroupRegistry.runtime_mode(active) == "active"
    assert restarted.access_allowed(-1001, True)
    assert [entry["reason"] for entry in active["runtime_history"]] == ["owner_paused", "owner_resumed"]


def test_hard_revoke_requires_review_after_member_readds_bot(tmp_path) -> None:
    registry = GroupRegistry(tmp_path, "owner_name", "approval")
    assert registry.bind_owner(42, "owner_name") == "claimed"
    group = registry.record_presence(
        -1001,
        title="Approved",
        chat_type="supergroup",
        telegram_status="member",
        approval_required=True,
    )
    registry.resolve_request(group["request_id"], "approved", 42)
    result, revoked = registry.set_runtime_mode(-1001, "hard", 42, expected_revision=0)
    assert result == "changed"
    registry.mark_leave_attempt(-1001, None)

    readded = registry.record_presence(
        -1001,
        title="Approved",
        chat_type="supergroup",
        telegram_status="member",
        added_by=_user(7, "member"),
        approval_required=True,
        membership_started=True,
    )

    assert GroupRegistry.runtime_mode(revoked) == "hard"
    assert readded["access_status"] == "pending"
    assert GroupRegistry.runtime_mode(readded) == "active"
    assert not registry.access_allowed(-1001, True)


def test_bound_owner_readding_hard_revoked_group_restores_access(tmp_path) -> None:
    registry = GroupRegistry(tmp_path, "owner_name", "approval")
    assert registry.bind_owner(42, "owner_name") == "claimed"
    group = registry.record_presence(
        -1001,
        title="Approved",
        chat_type="supergroup",
        telegram_status="member",
        approval_required=True,
    )
    registry.resolve_request(group["request_id"], "approved", 42)
    registry.set_runtime_mode(-1001, "hard", 42, expected_revision=0)
    registry.mark_leave_attempt(-1001, None)

    readded = registry.record_presence(
        -1001,
        title="Approved",
        chat_type="supergroup",
        telegram_status="administrator",
        added_by=_user(42, "renamed_owner"),
        approval_required=True,
        membership_started=True,
    )

    assert readded["access_status"] == "approved"
    assert readded["resolution"] == "readded_by_owner"
    assert GroupRegistry.runtime_mode(readded) == "active"
    assert registry.access_allowed(-1001, True)


def test_bootstrap_approves_only_api_verified_active_membership(tmp_path) -> None:
    registry = GroupRegistry(tmp_path, "owner_name", "approval")

    active = registry.record_bootstrap_result(
        -1001,
        title="Active",
        chat_type="supergroup",
        telegram_status="administrator",
    )
    inactive = registry.record_bootstrap_result(
        -1002,
        title="Left",
        chat_type="supergroup",
        telegram_status="left",
    )
    failed = registry.record_bootstrap_result(-1003, error="NetworkError")
    unverified_type = registry.record_bootstrap_result(-1004, telegram_status="member")

    assert active["access_status"] == "approved"
    assert inactive["access_status"] == "unreviewed"
    assert failed["access_status"] == "unreviewed"
    assert unverified_type["access_status"] == "unreviewed"
    assert registry.bootstrap_completed(-1001)
    assert registry.bootstrap_completed(-1002)
    assert not registry.bootstrap_completed(-1003)
    assert not registry.bootstrap_completed(-1004)


def test_failed_bootstrap_verification_preserves_confirmed_inactive_membership(tmp_path) -> None:
    registry = GroupRegistry(tmp_path, "owner_name", "approval")
    registry.record_bootstrap_result(
        -1001,
        title="Gone",
        chat_type="supergroup",
        telegram_status="left",
    )

    refreshed = registry.record_bootstrap_result(-1001, error="TemporaryTelegramError")

    assert refreshed["telegram_status"] == "left"
    assert refreshed["bootstrap"]["last_error"] == "TemporaryTelegramError"


def test_chat_migration_keeps_approval_and_removes_old_record(tmp_path) -> None:
    registry = GroupRegistry(tmp_path, "owner_name", "approval")
    group = registry.record_presence(
        -123,
        title="Basic group",
        chat_type="group",
        telegram_status="member",
        approval_required=True,
    )
    registry.resolve_request(group["request_id"], "approved", 42)
    registry.set_runtime_mode(-123, "soft", 42, expected_revision=0)

    registry.migrate_chat_id(-123, -100123)

    assert registry.get_group(-123) is None
    migrated = registry.get_group(-100123)
    assert migrated["access_status"] == "approved"
    assert GroupRegistry.runtime_mode(migrated) == "soft"
    assert GroupRegistry.runtime_revision(migrated) == 1
    assert migrated["type"] == "supergroup"


def test_corrupt_primary_store_recovers_a_valid_groups_backup(tmp_path) -> None:
    registry = GroupRegistry(tmp_path, "owner_name", "approval")
    registry.record_presence(
        -1001,
        title="Pending",
        chat_type="supergroup",
        telegram_status="member",
        approval_required=True,
    )
    registry.claim_group_notice(-1001)
    (tmp_path / "groups.json").write_text("{broken", encoding="utf-8")

    recovered = GroupRegistry(tmp_path, "owner_name", "approval")

    assert recovered.get_group(-1001)["access_status"] == "pending"
    assert list(tmp_path.glob("groups.json.corrupt-*"))
