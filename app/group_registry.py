from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.storage import JsonFile

ACTIVE_TELEGRAM_STATUSES = frozenset({"member", "administrator"})
BLOCKED_ACCESS_STATUSES = frozenset({"rejected", "expired"})


def normalize_username(value: str | None) -> str:
    return str(value or "").strip().lstrip("@").lower()


def normalize_telegram_status(value: str | None) -> str:
    status = str(value or "").strip().lower()
    if status in {"administrator", "creator"}:
        return "administrator"
    if status in {"member", "restricted"}:
        return "member"
    if status in {"left", "kicked"}:
        return status
    return "unknown"


def _utc_now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timestamp(now: datetime | None = None) -> str:
    return _utc_now(now).isoformat()


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _user_snapshot(user: Any | None) -> dict[str, Any] | None:
    if user is None:
        return None
    try:
        user_id = int(user.id)
    except (TypeError, ValueError, AttributeError):
        return None
    return {
        "id": user_id,
        "username": normalize_username(getattr(user, "username", None)) or None,
        "first_name": str(getattr(user, "first_name", "") or "").strip() or None,
        "last_name": str(getattr(user, "last_name", "") or "").strip() or None,
    }


class GroupRegistry:
    """Persistent membership and approval state, separate from chat preferences."""

    def __init__(self, data_dir: Path, configured_owner_username: str = "", access_mode: str = "open") -> None:
        self.store = JsonFile(
            data_dir / "groups.json",
            lambda: {
                "version": 1,
                "owner": {
                    "configured_username": normalize_username(configured_owner_username),
                    "user_id": None,
                    "bound_at": None,
                },
                "policy": {"mode": access_mode},
                "groups": {},
            },
        )
        self.configure(configured_owner_username, access_mode)

    def configure(self, owner_username: str, access_mode: str) -> None:
        normalized = normalize_username(owner_username)

        def mutate(data: dict[str, Any]) -> None:
            data["version"] = 1
            owner = data.get("owner")
            if not isinstance(owner, dict):
                owner = {}
                data["owner"] = owner
            owner["configured_username"] = normalized
            if not isinstance(owner.get("user_id"), int):
                owner["user_id"] = None
            if not isinstance(owner.get("bound_at"), str):
                owner["bound_at"] = None
            policy = data.get("policy")
            if not isinstance(policy, dict):
                policy = {}
                data["policy"] = policy
            policy["mode"] = access_mode
            if not isinstance(data.get("groups"), dict):
                data["groups"] = {}

        self.store.update(mutate)

    def snapshot(self) -> dict[str, Any]:
        return self.store.snapshot()

    def owner_id(self) -> int | None:
        value = self.store.snapshot().get("owner", {}).get("user_id")
        return value if isinstance(value, int) else None

    def is_owner(self, user_id: int) -> bool:
        return self.owner_id() == int(user_id)

    def bind_owner(self, user_id: int, username: str | None, *, now: datetime | None = None) -> str:
        result = "username_mismatch"
        normalized = normalize_username(username)

        def mutate(data: dict[str, Any]) -> None:
            nonlocal result
            owner = data.setdefault("owner", {})
            bound_id = owner.get("user_id")
            if isinstance(bound_id, int):
                result = "owner" if bound_id == int(user_id) else "different_owner"
                return
            if not normalized or normalized != normalize_username(owner.get("configured_username")):
                return
            owner["user_id"] = int(user_id)
            owner["bound_at"] = _timestamp(now)
            owner["bound_username"] = normalized
            result = "claimed"

        self.store.update(mutate)
        return result

    @staticmethod
    def _new_group(chat_id: int, timestamp: str) -> dict[str, Any]:
        return {
            "chat_id": int(chat_id),
            "title": None,
            "type": "unknown",
            "telegram_status": "unknown",
            "access_status": "unreviewed",
            "added_by": None,
            "first_seen_at": timestamp,
            "last_seen_at": timestamp,
            "status_changed_at": timestamp,
            "pending_since": None,
            "request_id": None,
            "owner_notification": None,
        }

    @staticmethod
    def _start_pending(group: dict[str, Any], timestamp: str) -> None:
        group["access_status"] = "pending"
        group["pending_since"] = timestamp
        group["request_id"] = uuid.uuid4().hex[:20]
        group["owner_notification"] = {
            "status": "pending",
            "attempts": 0,
            "last_attempt_at": None,
            "sent_at": None,
        }
        group.pop("resolved_at", None)
        group.pop("resolved_by", None)
        group.pop("resolution", None)
        group["group_notice"] = {
            "status": "pending",
            "attempts": 0,
            "last_attempt_at": None,
            "sent_at": None,
        }

    @staticmethod
    def _approve(group: dict[str, Any], timestamp: str, *, reason: str, owner_id: int | None = None) -> None:
        group["access_status"] = "approved"
        group["resolved_at"] = timestamp
        group["resolution"] = reason
        if owner_id is not None:
            group["resolved_by"] = int(owner_id)
        group["pending_since"] = None
        group["owner_notification"] = None

    def record_presence(
        self,
        chat_id: int,
        *,
        title: str | None,
        chat_type: str | None,
        telegram_status: str | None,
        added_by: Any | None = None,
        approval_required: bool,
        membership_started: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = _utc_now(now)
        timestamp = current.isoformat()
        normalized_status = normalize_telegram_status(telegram_status)
        actor = _user_snapshot(added_by)
        result: dict[str, Any] = {}

        def mutate(data: dict[str, Any]) -> None:
            nonlocal result
            groups = data.setdefault("groups", {})
            key = str(int(chat_id))
            existing = groups.get(key)
            group = existing if isinstance(existing, dict) else self._new_group(chat_id, timestamp)
            groups[key] = group
            previous_telegram_status = normalize_telegram_status(group.get("telegram_status"))
            previous_title = group.get("title")
            previous_type = group.get("type")
            group["chat_id"] = int(chat_id)
            clean_title = str(title or "").strip()
            if clean_title:
                group["title"] = clean_title
            clean_type = str(chat_type or "").strip().lower()
            if clean_type in {"group", "supergroup"}:
                group["type"] = clean_type
            last_seen = _parse_timestamp(group.get("last_seen_at"))
            metadata_changed = previous_title != group.get("title") or previous_type != group.get("type")
            if (
                last_seen is None
                or current - last_seen >= timedelta(minutes=5)
                or metadata_changed
                or previous_telegram_status != normalized_status
                or membership_started
            ):
                group["last_seen_at"] = timestamp
            if previous_telegram_status != normalized_status:
                group["status_changed_at"] = timestamp
            group["telegram_status"] = normalized_status
            if actor is not None and membership_started:
                group["added_by"] = actor

            access_status = str(group.get("access_status") or "unreviewed")
            if normalized_status in ACTIVE_TELEGRAM_STATUSES:
                owner_id = data.get("owner", {}).get("user_id")
                actor_is_owner = (
                    membership_started and actor is not None and isinstance(owner_id, int) and actor["id"] == owner_id
                )
                if access_status == "approved":
                    pass
                elif actor_is_owner:
                    self._approve(group, timestamp, reason="added_by_owner", owner_id=owner_id)
                elif approval_required:
                    should_restart = membership_started and access_status in BLOCKED_ACCESS_STATUSES
                    if access_status not in {"pending", *BLOCKED_ACCESS_STATUSES} or should_restart:
                        self._start_pending(group, timestamp)
            elif normalized_status in {"left", "kicked"} and access_status == "pending":
                group["access_status"] = "expired"
                group["resolved_at"] = timestamp
                group["resolution"] = "left_before_review"
                group["pending_since"] = None
                group["owner_notification"] = None
            result = copy.deepcopy(group)

        self.store.update(mutate)
        return result

    def access_allowed(self, chat_id: int, approval_required: bool) -> bool:
        if not approval_required:
            return True
        group = self.store.snapshot().get("groups", {}).get(str(int(chat_id)))
        return isinstance(group, dict) and group.get("access_status") == "approved"

    def get_group(self, chat_id: int) -> dict[str, Any] | None:
        group = self.store.snapshot().get("groups", {}).get(str(int(chat_id)))
        return group if isinstance(group, dict) else None

    def claim_pending_notifications(
        self,
        *,
        retry_after_seconds: int = 300,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically reserve due notifications so concurrent handlers cannot duplicate them."""
        current = _utc_now(now)
        timestamp = current.isoformat()
        claimed: list[dict[str, Any]] = []

        def mutate(data: dict[str, Any]) -> None:
            for group in data.setdefault("groups", {}).values():
                if not isinstance(group, dict) or group.get("access_status") != "pending":
                    continue
                notification = group.get("owner_notification")
                if not isinstance(notification, dict):
                    notification = {"status": "pending", "attempts": 0, "last_attempt_at": None, "sent_at": None}
                    group["owner_notification"] = notification
                status = notification.get("status")
                if status == "sent":
                    continue
                last_attempt = _parse_timestamp(notification.get("last_attempt_at"))
                if (
                    status in {"failed", "sending"}
                    and last_attempt is not None
                    and current - last_attempt < timedelta(seconds=retry_after_seconds)
                ):
                    continue
                notification["status"] = "sending"
                notification["last_attempt_at"] = timestamp
                claimed.append(copy.deepcopy(group))

        self.store.update(mutate)
        return claimed

    def claim_group_notice(
        self,
        chat_id: int,
        *,
        retry_after_seconds: int = 300,
        now: datetime | None = None,
    ) -> bool:
        claimed = False
        current = _utc_now(now)
        timestamp = current.isoformat()

        def mutate(data: dict[str, Any]) -> None:
            nonlocal claimed
            group = data.setdefault("groups", {}).get(str(int(chat_id)))
            if not isinstance(group, dict) or group.get("access_status") != "pending":
                return
            notice = group.get("group_notice")
            if not isinstance(notice, dict):
                notice = {"status": "pending", "attempts": 0, "last_attempt_at": None, "sent_at": None}
                group["group_notice"] = notice
            status = notice.get("status")
            if status == "sent":
                return
            last_attempt = _parse_timestamp(notice.get("last_attempt_at"))
            if (
                status in {"failed", "sending"}
                and last_attempt is not None
                and current - last_attempt < timedelta(seconds=retry_after_seconds)
            ):
                return
            notice["status"] = "sending"
            notice["last_attempt_at"] = timestamp
            claimed = True

        self.store.update(mutate)
        return claimed

    def mark_group_notice(self, chat_id: int, success: bool, *, now: datetime | None = None) -> None:
        timestamp = _timestamp(now)

        def mutate(data: dict[str, Any]) -> None:
            group = data.setdefault("groups", {}).get(str(int(chat_id)))
            if not isinstance(group, dict) or group.get("access_status") != "pending":
                return
            notice = group.get("group_notice")
            if not isinstance(notice, dict):
                notice = {"status": "pending", "attempts": 0, "last_attempt_at": None, "sent_at": None}
                group["group_notice"] = notice
            notice["attempts"] = int(notice.get("attempts") or 0) + 1
            notice["last_attempt_at"] = timestamp
            notice["status"] = "sent" if success else "failed"
            if success:
                notice["sent_at"] = timestamp

        self.store.update(mutate)

    def mark_notification(self, request_id: str, success: bool, *, now: datetime | None = None) -> bool:
        found = False
        timestamp = _timestamp(now)

        def mutate(data: dict[str, Any]) -> None:
            nonlocal found
            for group in data.setdefault("groups", {}).values():
                if not isinstance(group, dict) or group.get("request_id") != request_id:
                    continue
                if group.get("access_status") != "pending":
                    return
                notification = group.get("owner_notification")
                if not isinstance(notification, dict):
                    notification = {"status": "pending", "attempts": 0, "last_attempt_at": None, "sent_at": None}
                    group["owner_notification"] = notification
                notification["attempts"] = int(notification.get("attempts") or 0) + 1
                notification["last_attempt_at"] = timestamp
                notification["status"] = "sent" if success else "failed"
                if success:
                    notification["sent_at"] = timestamp
                found = True
                return

        self.store.update(mutate)
        return found

    def resolve_request(
        self,
        request_id: str,
        decision: str,
        owner_id: int,
        *,
        now: datetime | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("unsupported group decision")
        result = "not_found"
        resolved_group: dict[str, Any] | None = None
        timestamp = _timestamp(now)

        def mutate(data: dict[str, Any]) -> None:
            nonlocal result, resolved_group
            for group in data.setdefault("groups", {}).values():
                if not isinstance(group, dict) or group.get("request_id") != request_id:
                    continue
                if group.get("access_status") == "pending":
                    if decision == "approved":
                        self._approve(group, timestamp, reason="owner_approved", owner_id=owner_id)
                    else:
                        group["access_status"] = "rejected"
                        group["resolved_at"] = timestamp
                        group["resolved_by"] = int(owner_id)
                        group["resolution"] = "owner_rejected"
                        group["pending_since"] = None
                        group["owner_notification"] = None
                    result = "resolved"
                else:
                    result = "already_resolved"
                resolved_group = copy.deepcopy(group)
                return

        self.store.update(mutate)
        return result, resolved_group

    def approve_pending_added_by(self, owner_id: int, *, now: datetime | None = None) -> list[dict[str, Any]]:
        approved: list[dict[str, Any]] = []
        timestamp = _timestamp(now)

        def mutate(data: dict[str, Any]) -> None:
            for group in data.setdefault("groups", {}).values():
                if not isinstance(group, dict) or group.get("access_status") != "pending":
                    continue
                if normalize_telegram_status(group.get("telegram_status")) not in ACTIVE_TELEGRAM_STATUSES:
                    continue
                added_by = group.get("added_by")
                if not isinstance(added_by, dict) or added_by.get("id") != int(owner_id):
                    continue
                self._approve(group, timestamp, reason="added_by_owner", owner_id=owner_id)
                approved.append(copy.deepcopy(group))

        self.store.update(mutate)
        return approved

    def expire_pending(self, ttl_hours: int, *, now: datetime | None = None) -> list[dict[str, Any]]:
        current = _utc_now(now)
        expired: list[dict[str, Any]] = []
        timestamp = current.isoformat()

        def mutate(data: dict[str, Any]) -> None:
            for group in data.setdefault("groups", {}).values():
                if not isinstance(group, dict) or group.get("access_status") != "pending":
                    continue
                pending_since = _parse_timestamp(group.get("pending_since"))
                if pending_since is not None and current - pending_since < timedelta(hours=ttl_hours):
                    continue
                group["access_status"] = "expired"
                group["resolved_at"] = timestamp
                group["resolution"] = "approval_timeout"
                group["pending_since"] = None
                group["owner_notification"] = None
                expired.append(copy.deepcopy(group))

        self.store.update(mutate)
        return expired

    def current_groups(self) -> list[dict[str, Any]]:
        groups = [
            group
            for group in self.store.snapshot().get("groups", {}).values()
            if isinstance(group, dict)
            and isinstance(group.get("chat_id"), int)
            and normalize_telegram_status(group.get("telegram_status")) in ACTIVE_TELEGRAM_STATUSES
        ]
        return sorted(groups, key=lambda item: (str(item.get("title") or "").casefold(), int(item.get("chat_id") or 0)))

    def all_groups(self) -> list[dict[str, Any]]:
        groups = [
            group
            for group in self.store.snapshot().get("groups", {}).values()
            if isinstance(group, dict) and isinstance(group.get("chat_id"), int)
        ]
        return sorted(groups, key=lambda item: (str(item.get("title") or "").casefold(), int(item.get("chat_id") or 0)))

    def pending_groups(self) -> list[dict[str, Any]]:
        return [group for group in self.all_groups() if group.get("access_status") == "pending"]

    def blocked_groups_for_leave(
        self,
        *,
        retry_after_seconds: int = 300,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        current = _utc_now(now)
        result: list[dict[str, Any]] = []
        for group in self.current_groups():
            if group.get("access_status") not in BLOCKED_ACCESS_STATUSES:
                continue
            last_attempt = _parse_timestamp(group.get("leave_attempted_at"))
            if last_attempt is None or current - last_attempt >= timedelta(seconds=retry_after_seconds):
                result.append(group)
        return result

    def bootstrap_completed(self, chat_id: int) -> bool:
        group = self.get_group(chat_id)
        bootstrap = group.get("bootstrap") if isinstance(group, dict) else None
        return isinstance(bootstrap, dict) and isinstance(bootstrap.get("checked_at"), str)

    def record_bootstrap_result(
        self,
        chat_id: int,
        *,
        title: str | None = None,
        chat_type: str | None = None,
        telegram_status: str = "unknown",
        error: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = _timestamp(now)
        status = normalize_telegram_status(telegram_status)
        result: dict[str, Any] = {}

        def mutate(data: dict[str, Any]) -> None:
            nonlocal result
            groups = data.setdefault("groups", {})
            key = str(int(chat_id))
            group = groups.get(key)
            if not isinstance(group, dict):
                group = self._new_group(chat_id, timestamp)
                groups[key] = group
            clean_title = str(title or "").strip()
            if clean_title:
                group["title"] = clean_title
            normalized_type = str(chat_type or "").strip().lower()
            if normalized_type in {"group", "supergroup"}:
                group["type"] = normalized_type
            group["last_seen_at"] = timestamp
            group["telegram_status"] = status
            bootstrap = group.get("bootstrap")
            if not isinstance(bootstrap, dict):
                bootstrap = {}
                group["bootstrap"] = bootstrap
            bootstrap["last_attempt_at"] = timestamp
            if error:
                bootstrap["last_error"] = str(error)[:500]
            else:
                bootstrap["status"] = status
                valid_active_group = status in ACTIVE_TELEGRAM_STATUSES and group.get("type") in {
                    "group",
                    "supergroup",
                }
                definitive_inactive = status in {"left", "kicked"}
                if valid_active_group or definitive_inactive:
                    bootstrap["checked_at"] = timestamp
                    bootstrap.pop("last_error", None)
                else:
                    bootstrap.pop("checked_at", None)
                    bootstrap["last_error"] = "membership or group type could not be verified"
                if valid_active_group:
                    self._approve(group, timestamp, reason="configured_bootstrap")
            result = copy.deepcopy(group)

        self.store.update(mutate)
        return result

    def mark_leave_attempt(self, chat_id: int, error: str | None, *, now: datetime | None = None) -> None:
        timestamp = _timestamp(now)

        def mutate(data: dict[str, Any]) -> None:
            group = data.setdefault("groups", {}).get(str(int(chat_id)))
            if not isinstance(group, dict):
                return
            group["leave_attempted_at"] = timestamp
            if error:
                group["leave_error"] = str(error)[:500]
            else:
                group["telegram_status"] = "left"
                group["status_changed_at"] = timestamp
                group.pop("leave_error", None)

        self.store.update(mutate)

    def migrate_chat_id(self, old_chat_id: int, new_chat_id: int, *, now: datetime | None = None) -> None:
        if int(old_chat_id) == int(new_chat_id):
            return
        timestamp = _timestamp(now)

        def mutate(data: dict[str, Any]) -> None:
            groups = data.setdefault("groups", {})
            source = groups.pop(str(int(old_chat_id)), None)
            if not isinstance(source, dict):
                return
            target = groups.get(str(int(new_chat_id)))
            if not isinstance(target, dict):
                target = source
            else:
                if not target.get("title") and source.get("title"):
                    target["title"] = source["title"]
                if not isinstance(target.get("added_by"), dict) and isinstance(source.get("added_by"), dict):
                    target["added_by"] = copy.deepcopy(source["added_by"])
                source_status = str(source.get("access_status") or "unreviewed")
                target_status = str(target.get("access_status") or "unreviewed")
                priority = {"unreviewed": 0, "expired": 1, "rejected": 1, "pending": 2, "approved": 3}
                if priority.get(source_status, 0) > priority.get(target_status, 0):
                    access_fields = (
                        "access_status",
                        "pending_since",
                        "request_id",
                        "owner_notification",
                        "group_notice",
                        "resolved_at",
                        "resolved_by",
                        "resolution",
                    )
                    for field in access_fields:
                        if field in source:
                            target[field] = copy.deepcopy(source[field])
                        else:
                            target.pop(field, None)
                if normalize_telegram_status(target.get("telegram_status")) == "unknown":
                    target["telegram_status"] = normalize_telegram_status(source.get("telegram_status"))
            target["chat_id"] = int(new_chat_id)
            target["type"] = "supergroup"
            target["last_seen_at"] = timestamp
            target["migrated_from_chat_id"] = int(old_chat_id)
            target["migrated_at"] = timestamp
            groups[str(int(new_chat_id))] = target

        self.store.update(mutate)
