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
