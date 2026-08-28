from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import main
from app.settings import Settings


class GroupBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, dict]] = []
        self.reactions: list[tuple[int, int, str | None]] = []
        self.left: list[int] = []
        self.callback_answers: list[tuple[object, str, bool]] = []
        self.command_sets: list[tuple[list, object]] = []
        self.members: dict[int, object] = {}
        self.chats: dict[int, object] = {}
        self.fail_owner_cards = False
        self.fail_leave = False

    def send_message(self, chat_id, text, **kwargs):
        if self.fail_owner_cards and kwargs.get("reply_markup") is not None:
            raise RuntimeError("notification failed")
        self.messages.append((int(chat_id), text, kwargs))
        return SimpleNamespace(message_id=len(self.messages))

    def set_message_reaction(self, chat_id, message_id, reaction, **_kwargs):
        emoji = reaction[0].emoji if reaction else None
        self.reactions.append((int(chat_id), int(message_id), emoji))
        return True

    def get_chat_member(self, chat_id, _user_id):
        value = self.members.get(int(chat_id), SimpleNamespace(status="member", can_delete_messages=False))
        if isinstance(value, Exception):
            raise value
        return value

    def get_chat(self, chat_id):
        value = self.chats.get(
            int(chat_id),
            SimpleNamespace(id=int(chat_id), title=f"Group {chat_id}", type="supergroup"),
        )
        if isinstance(value, Exception):
            raise value
        return value

    def leave_chat(self, chat_id):
        if self.fail_leave:
            raise RuntimeError("leave failed")
        self.left.append(int(chat_id))
        return True

    def answer_callback_query(self, callback_id, *, text, show_alert=False):
        self.callback_answers.append((callback_id, text, show_alert))

    def edit_message_reply_markup(self, *_args, **_kwargs):
        return True

    def set_my_commands(self, commands, scope=None, **_kwargs):
        self.command_sets.append((commands, scope))
        return True


def _settings(tmp_path: Path, **changes) -> Settings:
    settings = Settings(
        token="123456:abcdefghijklmnopqrstuvwxyz",
        logs_chat_id=None,
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "cache",
        logs_dir=tmp_path / "logs",
        cookies_file=None,
        max_filesize=50_000_000,
        workers=1,
        max_queue=20,
        upload_workers=1,
        concurrent_fragments=2,
        job_timeout=60,
        disk_cache_max_files=5,
        disk_cache_ttl=300,
        file_id_cache_max_items=500,
        file_id_cache_ttl_days=30,
        media_cache_enabled=True,
        status_reactions=True,
        delete_original=True,
        default_language="en",
        log_level="INFO",
        group_access_mode="approval",
        group_owner_username="owner_name",
        pending_group_ttl_hours=168,
    )
    return replace(settings, **changes)


def _user(user_id: int, username: str, *, is_bot: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        username=username,
        first_name="Test",
        last_name="User",
        is_bot=is_bot,
    )


def _message(chat_id: int = -1001, user_id: int = 7) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, title="Private test group", type="supergroup"),
        from_user=_user(user_id, f"user_{user_id}"),
        text="https://example.com/video",
        caption=None,
        message_id=42,
        message_thread_id=None,
    )


def _membership_update(chat_id: int, actor: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, title="New group", type="supergroup"),
        from_user=actor,
        old_chat_member=SimpleNamespace(status="left"),
        new_chat_member=SimpleNamespace(status="member"),
    )


def _callback(request_id: str, user_id: int, action: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"callback-{user_id}-{action}",
        data=f"ga:{action}:{request_id}",
        from_user=_user(user_id, f"user_{user_id}"),
        message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=9),
    )


def test_pending_group_is_blocked_before_url_validation_reaction_and_queue(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    bot = GroupBot()
    app.bot = bot
    monkeypatch.setattr(main, "validate_public_url", lambda _url: (_ for _ in ()).throw(AssertionError("URL parsed")))

    app._handle_group_message(_message())
    app._handle_group_message(_message())

    group = app.group_registry.get_group(-1001)
    assert group["access_status"] == "pending"
    assert app.queue.empty()
    assert bot.reactions == []
    assert [chat_id for chat_id, _text, _kwargs in bot.messages] == [-1001]


def test_observed_messages_do_not_downgrade_a_known_administrator_status(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    app.group_registry.record_bootstrap_result(
        -1001,
        title="Admin group",
        chat_type="supergroup",
        telegram_status="administrator",
    )

    assert app._record_group_access(SimpleNamespace(id=-1001, title="Admin group", type="supergroup"))
    assert app.group_registry.get_group(-1001)["telegram_status"] == "administrator"


def test_owner_binding_flushes_pending_request_and_callback_is_private_and_idempotent(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    bot = GroupBot()
    app.bot = bot
    app.bot_id = 500
    app.bot_username = "downloader"
    app._handle_my_chat_member(_membership_update(-1001, _user(7, "adder")))
    group = app.group_registry.get_group(-1001)
    request_id = group["request_id"]
    assert not any(chat_id == 42 for chat_id, _text, _kwargs in bot.messages)

    assert not app._maybe_bind_owner(_user(99, "wrong_user"))
    assert app._maybe_bind_owner(_user(42, "OWNER_NAME"))
    owner_cards = [item for item in bot.messages if item[0] == 42 and item[2].get("reply_markup") is not None]
    assert len(owner_cards) == 1
    assert "New group" in owner_cards[0][1]
    assert "@adder" in owner_cards[0][1]

    app._handle_group_access_callback(_callback(request_id, 99, "a"))
    assert app.group_registry.get_group(-1001)["access_status"] == "pending"
    assert bot.callback_answers[-1][2] is True

    app._handle_group_access_callback(_callback(request_id, 42, "a"))
    assert app.group_registry.get_group(-1001)["access_status"] == "approved"
    app._handle_group_access_callback(_callback(request_id, 42, "r"))
    assert app.group_registry.get_group(-1001)["access_status"] == "approved"
    assert bot.left == []
    assert "Already approved" in bot.callback_answers[-1][1]


def test_bound_owner_adds_group_without_an_approval_round_trip(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    bot = GroupBot()
    app.bot = bot
    app.bot_id = 500
    app.bot_username = "downloader"
    assert app._maybe_bind_owner(_user(42, "owner_name"))
    bot.messages.clear()

    app._handle_my_chat_member(_membership_update(-1002, _user(42, "renamed_owner")))

    assert app.group_registry.get_group(-1002)["access_status"] == "approved"
    assert not any(kwargs.get("reply_markup") is not None for _chat_id, _text, kwargs in bot.messages)
    assert any(chat_id == -1002 for chat_id, _text, _kwargs in bot.messages)


def test_rejection_blocks_access_even_when_leave_fails_and_retry_stays_quiet(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    bot = GroupBot()
    bot.fail_leave = True
    app.bot = bot
    app.bot_id = 500
    app._maybe_bind_owner(_user(42, "owner_name"))
    app._handle_my_chat_member(_membership_update(-1003, _user(7, "adder")))
    request_id = app.group_registry.get_group(-1003)["request_id"]

    app._handle_group_access_callback(_callback(request_id, 42, "r"))
    monkeypatch.setattr(main, "validate_public_url", lambda _url: (_ for _ in ()).throw(AssertionError("URL parsed")))
    app._handle_group_message(_message(-1003))

    group = app.group_registry.get_group(-1003)
    assert group["access_status"] == "rejected"
    assert group["telegram_status"] == "member"
    assert group["leave_error"] == "RuntimeError"
    assert app.queue.empty()
    assert bot.reactions == []


def test_stale_approval_button_cannot_reject_a_group_after_policy_is_disabled(tmp_path) -> None:
    approval_app = main.BotApplication(_settings(tmp_path))
    approval_app.bot = GroupBot()
    approval_app._maybe_bind_owner(_user(42, "owner_name"))
    approval_app._handle_my_chat_member(_membership_update(-1006, _user(7, "adder")))
    request_id = approval_app.group_registry.get_group(-1006)["request_id"]

    open_app = main.BotApplication(_settings(tmp_path, group_access_mode="open", group_owner_username=""))
    bot = GroupBot()
    open_app.bot = bot
    open_app._handle_group_access_callback(_callback(request_id, 42, "r"))

    assert open_app.group_registry.get_group(-1006)["access_status"] == "pending"
    assert bot.left == []
    assert bot.callback_answers[-1][2] is True


def test_expired_group_is_persistently_blocked_and_left(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path, pending_group_ttl_hours=1))
    bot = GroupBot()
    app.bot = bot

    app.group_registry.record_presence(
        -1004,
        title="Old pending group",
        chat_type="supergroup",
        telegram_status="member",
        approval_required=True,
        now=datetime.now(UTC) - timedelta(hours=2),
    )

    app._maintain_group_access()

    group = app.group_registry.get_group(-1004)
    assert group["access_status"] == "expired"
    assert group["telegram_status"] == "left"
    assert bot.left == [-1004]


def test_bootstrap_approves_only_active_configured_ids_and_legacy_groups_stay_pending(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path, group_bootstrap_chat_ids=(-1001, -1002, -1003)))
    bot = GroupBot()
    bot.members = {
        -1001: SimpleNamespace(status="administrator"),
        -1002: SimpleNamespace(status="left"),
        -1003: RuntimeError("temporary API error"),
        -1004: SimpleNamespace(status="member"),
    }
    app.bot = bot
    app.bot_id = 500
    app.storage.set_chat_language(-1004, "en")

    app._refresh_group_registry()

    assert app.group_registry.get_group(-1001)["access_status"] == "approved"
    assert app.group_registry.get_group(-1002)["access_status"] == "unreviewed"
    assert app.group_registry.get_group(-1003)["access_status"] == "unreviewed"
    assert not app.group_registry.bootstrap_completed(-1003)
    assert app.group_registry.get_group(-1004)["access_status"] == "pending"


def test_failed_owner_notification_is_persisted_and_can_retry_after_restart(tmp_path, monkeypatch) -> None:
    app = main.BotApplication(_settings(tmp_path))
    failing_bot = GroupBot()
    failing_bot.fail_owner_cards = True
    app.bot = failing_bot
    app.bot_id = 500
    app._maybe_bind_owner(_user(42, "owner_name"))
    app._handle_my_chat_member(_membership_update(-1005, _user(7, "adder")))
    notification = app.group_registry.get_group(-1005)["owner_notification"]
    assert notification["status"] == "failed"

    restarted = main.BotApplication(_settings(tmp_path))
    working_bot = GroupBot()
    restarted.bot = working_bot
    monkeypatch.setattr(main, "GROUP_NOTIFICATION_RETRY_SECONDS", 0)
    restarted._flush_pending_group_notifications()

    cards = [item for item in working_bot.messages if item[2].get("reply_markup") is not None]
    assert len(cards) == 1
    assert restarted.group_registry.get_group(-1005)["owner_notification"]["status"] == "sent"


def test_owner_commands_are_scoped_and_not_in_the_global_menu(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))
    bot = GroupBot()
    app.bot = bot
    app._maybe_bind_owner(_user(42, "owner_name"))
    bot.command_sets.clear()

    app._set_commands()

    global_commands = [command.command for command in bot.command_sets[0][0]]
    owner_commands = [command.command for command in bot.command_sets[1][0]]
    assert "groups" not in global_commands
    assert "pending_groups" not in global_commands
    assert {"groups", "pending_groups"}.issubset(owner_commands)
    assert bot.command_sets[0][1] is None
    assert bot.command_sets[1][1].chat_id == 42


def test_membership_and_callback_handlers_are_registered(tmp_path) -> None:
    app = main.BotApplication(_settings(tmp_path))

    assert len(app.bot.my_chat_member_handlers) == 1
    assert len(app.bot.callback_query_handlers) == 1
