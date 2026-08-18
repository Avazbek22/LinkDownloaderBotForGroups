from __future__ import annotations

import os

from app import env_config
from app.download_backend import _youtube_player_clients
from app.settings import load_settings


def test_local_dotenv_refreshes_downloader_settings_and_legacy_client(tmp_path) -> None:
    names = (
        "BOT_TOKEN",
        "YTDLP_JS_RUNTIMES",
        "YTDLP_YOUTUBE_PLAYER_CLIENT",
        "YTDLP_YOUTUBE_PLAYER_CLIENTS",
    )
    previous = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ.pop(name, None)
    (tmp_path / ".env").write_text(
        "BOT_TOKEN=123456:test-token\nYTDLP_JS_RUNTIMES=deno\nYTDLP_YOUTUBE_PLAYER_CLIENT=ios\n",
        encoding="utf-8",
    )

    try:
        settings = load_settings(tmp_path)

        assert settings.token == "123456:test-token"
        assert env_config.YTDLP_JS_RUNTIMES == "deno"
        assert _youtube_player_clients() == ["ios"]
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        env_config.reload_from_environment()
