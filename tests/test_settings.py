from __future__ import annotations

import os

from app import env_config
from app.download_backend import _site_options, _youtube_player_clients
from app.settings import load_settings


def test_local_dotenv_refreshes_downloader_settings_and_legacy_client(tmp_path) -> None:
    names = (
        "BOT_TOKEN",
        "YTDLP_JS_RUNTIMES",
        "YTDLP_REMOTE_COMPONENTS",
        "YTDLP_YOUTUBE_PLAYER_CLIENT",
        "YTDLP_YOUTUBE_PLAYER_CLIENTS",
        "GROUP_ACCESS_MODE",
        "GROUP_OWNER_USERNAME",
        "PENDING_GROUP_TTL_HOURS",
        "GROUP_BOOTSTRAP_CHAT_IDS",
    )
    previous = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ.pop(name, None)
    (tmp_path / ".env").write_text(
        "BOT_TOKEN=123456:test-token\n"
        "YTDLP_JS_RUNTIMES=deno\n"
        "YTDLP_REMOTE_COMPONENTS=\n"
        "YTDLP_YOUTUBE_PLAYER_CLIENT=ios\n",
        encoding="utf-8",
    )

    try:
        settings = load_settings(tmp_path)

        assert settings.token == "123456:test-token"
        assert env_config.YTDLP_JS_RUNTIMES == "deno"
        assert env_config.YTDLP_REMOTE_COMPONENTS == ""
        assert "remote_components" not in _site_options("https://www.youtube.com/watch?v=video")
        assert _youtube_player_clients() == ["ios"]
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        env_config.reload_from_environment()


def test_group_approval_settings_are_normalized_and_deduplicated(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("GROUP_ACCESS_MODE", "APPROVAL")
    monkeypatch.setenv("GROUP_OWNER_USERNAME", "@Owner_Name")
    monkeypatch.setenv("PENDING_GROUP_TTL_HOURS", "72")
    monkeypatch.setenv("GROUP_BOOTSTRAP_CHAT_IDS", "-1001, -1002,-1001")

    settings = load_settings(tmp_path)

    assert settings.group_access_mode == "approval"
    assert settings.group_owner_username == "owner_name"
    assert settings.pending_group_ttl_hours == 72
    assert settings.group_bootstrap_chat_ids == (-1001, -1002)


def test_approval_mode_requires_a_valid_owner_username(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("GROUP_ACCESS_MODE", "approval")
    monkeypatch.delenv("GROUP_OWNER_USERNAME", raising=False)

    try:
        load_settings(tmp_path)
    except RuntimeError as exc:
        assert "GROUP_OWNER_USERNAME is required" in str(exc)
    else:
        raise AssertionError("approval mode accepted a missing owner")

    monkeypatch.setenv("GROUP_OWNER_USERNAME", "bad name")
    try:
        load_settings(tmp_path)
    except RuntimeError as exc:
        assert "valid Telegram username" in str(exc)
    else:
        raise AssertionError("approval mode accepted an invalid owner")


def test_group_access_defaults_to_backward_compatible_open_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    for name in (
        "GROUP_ACCESS_MODE",
        "GROUP_OWNER_USERNAME",
        "PENDING_GROUP_TTL_HOURS",
        "GROUP_BOOTSTRAP_CHAT_IDS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings(tmp_path)

    assert settings.group_access_mode == "open"
    assert settings.group_owner_username == ""
    assert settings.pending_group_ttl_hours == 168
    assert settings.group_bootstrap_chat_ids == ()


def test_unset_remote_components_keeps_default(monkeypatch) -> None:
    previous = os.environ.get("YTDLP_REMOTE_COMPONENTS")
    monkeypatch.delenv("YTDLP_REMOTE_COMPONENTS", raising=False)
    try:
        env_config.reload_from_environment()
        assert env_config.YTDLP_REMOTE_COMPONENTS == "ejs:github"
    finally:
        if previous is None:
            os.environ.pop("YTDLP_REMOTE_COMPONENTS", None)
        else:
            os.environ["YTDLP_REMOTE_COMPONENTS"] = previous
        env_config.reload_from_environment()
