# Link Downloader Bot for Telegram Groups

A quiet, self-hosted Telegram bot that replaces video links with the actual video. It is designed for small private groups: paste a supported link, and the bot downloads the video, posts it silently in the same topic, then removes the original message only after success.

The bot intentionally sends no “downloading” or failure messages for automatic requests. A 👀 reaction means the link is being processed; a retained link changes to 👎 on failure or 👍 after success. If reactions are unavailable in a chat, the bot keeps working silently. Technical reasons are written to the rotating log.

## Highlights

- Supports YouTube, Instagram, TikTok, VK, X, Facebook and many other sites through [yt-dlp](https://github.com/yt-dlp/yt-dlp).
- Silent messages and no link previews.
- Quiet status reactions instead of progress messages.
- Telegram forum topic support.
- English and Russian interface; English is the default.
- Per-user automatic-download opt-out.
- Safe public-URL validation against local and private network addresses.
- Bounded worker queue and concurrent downloads.
- Single-flight deduplication: simultaneous copies of the same video are downloaded once.
- Reuses Telegram `file_id`, avoiding repeated downloads and uploads.
- Five-minute disk cache with automatic cleanup.
- Atomic JSON storage with backups and migration from the legacy `prefs.json`.
- Daily rotating logs retained for 60 days.
- Reproducible Docker deployment with automatic application and yt-dlp updates, both with rollback.

## How it works

1. A member posts a video link.
2. After accepting the job, the bot reacts with 👀 and quietly downloads the video.
3. The bot publishes the video silently in the same chat topic.
4. After a successful upload, it removes the original link if it has permission; otherwise it changes the reaction to 👍.
5. If processing fails, the original link remains with a 👎 reaction.

When the same video is posted again, the bot normally sends the existing Telegram media by `file_id`. Captions and sender attribution are still generated independently for every group.

## Telegram setup

Create a bot with [@BotFather](https://t.me/BotFather), then:

1. Disable **Group Privacy** under **Bot Settings → Group Privacy**. Otherwise the bot cannot see ordinary group messages.
2. Add the bot to a group.
3. Grant **Delete messages** if original links should be removed.

No other administrator rights are required.

## Installation

On a clean Debian or Ubuntu VPS, clone the repository you want to follow and run its installer:

```bash
sudo git clone https://github.com/Avazbek22/LinkDownloaderBotForGroups.git /opt/linkdownloaderbot
sudo bash /opt/linkdownloaderbot/install.sh
```

The installer asks only for the Telegram bot token. It installs missing prerequisites, creates the local configuration, starts the bot, and enables safe automatic updates. Running it again updates the installation without replacing `.env`, `data/`, or `logs/`.

To run your own fork, use its clone URL instead:

```bash
sudo git clone https://github.com/YOUR_NAME/LinkDownloaderBotForGroups.git /opt/linkdownloaderbot
sudo bash /opt/linkdownloaderbot/install.sh
```

The installer detects the repository's `origin`, so a fork follows its own `main` branch and never the original owner's repository. The bot token remains only in `/opt/linkdownloaderbot/.env`; it is not sent to GitHub. The installer refuses automatic updates when tracked server files contain local changes.

For a manual Docker setup without the installer:

```bash
git clone https://github.com/Avazbek22/LinkDownloaderBotForGroups.git
cd LinkDownloaderBotForGroups
cp .env-example .env
nano .env
docker compose up -d --build
docker compose logs -f --tail=200
```

## Configuration

Only `BOT_TOKEN` is required.

| Variable | Default | Description |
|---|---:|---|
| `BOT_TOKEN` | — | Telegram bot token |
| `LOGS_CHAT_ID` | empty | Optional chat for future critical operational notifications |
| `MAX_FILESIZE` | `52428800` | Maximum upload size in bytes |
| `WORKERS` | `2` | Concurrent download workers |
| `MAX_QUEUE` | `200` | In-memory queue capacity |
| `UPLOAD_WORKERS` | `2` | Concurrent local-file uploads to Telegram |
| `JOB_TIMEOUT_SECONDS` | `900` | Download deadline |
| `DEFAULT_LANGUAGE` | `en` | Default UI language: `en` or `ru` |
| `DELETE_ORIGINAL` | `true` | Remove a link after successful delivery |
| `MEDIA_CACHE_ENABLED` | `true` | Enable disk and Telegram `file_id` caches |
| `STATUS_REACTIONS` | `true` | Use 👀 while processing and 👎/👍 for retained links |
| `DISK_CACHE_MAX_FILES` | `5` | Maximum recent media files on disk |
| `DISK_CACHE_TTL_SECONDS` | `300` | Disk-cache lifetime after last use |
| `FILE_ID_CACHE_MAX_ITEMS` | `500` | Maximum persistent Telegram media entries |
| `FILE_ID_CACHE_TTL_DAYS` | `30` | Telegram media-cache lifetime |
| `COOKIES_FILE` | empty | Optional cookies file; `/app/data/cookies.txt` is convenient in Docker |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `YTDLP_CONCURRENT_FRAGMENTS` | `4` | Concurrent fragments per download |

See [.env-example](.env-example) for yt-dlp site-specific options.

## Commands

- `/start` and `/help` — show instructions.
- `/en` or `/ru` — change the language; group administrators only.
- `/settings` — show the current group settings; group administrators only.
- `/delete_original on` or `/delete_original off` — configure successful-link deletion; group administrators only.
- `@BotName me` or `@BotName я` — toggle automatic downloads for yourself.
- When opted out, use `@BotName <link>` for a manual download.

The bot remains silent for ordinary automatic failures by design.

## Persistent data

Runtime state is stored in `data/`:

```text
data/
├── settings.json       # per-chat language and settings
├── users.json          # per-user opt-out choices
├── state.json          # welcome and migration state
├── media_cache.json    # Telegram file_id cache
└── cache/              # short-lived downloaded media
```

Writes use a temporary file, `fsync`, and atomic replacement. A last-known-good `.bak` file is retained. Invalid JSON is quarantined rather than silently overwritten. Existing `data/prefs.json` is imported once and left untouched as a fallback.

## Logs

Application logs are written to stdout and `logs/bot.log`. They rotate daily at UTC midnight, with 60 daily files retained. Query strings are omitted from logged URLs to reduce accidental exposure of tokens.

```bash
docker compose logs -f --tail=200
ls -la logs/
```

## Automatic yt-dlp updates

Video extractors change frequently. `install.sh` enables a nightly systemd timer when systemd is available. The updater:

1. preserves the current Docker image as a rollback image;
2. refreshes only the yt-dlp image layer;
3. runs an import/version smoke test;
4. recreates the service;
5. restores the previous image if the new container does not stay running.

Updater output is stored in daily `logs/updater-YYYY-MM-DD.log` files and retained for 60 days.

```bash
systemctl status linkdownloaderbotforgroups-yt-dlp-update.timer
sudo systemctl start linkdownloaderbotforgroups-yt-dlp-update.service
```

Other dependencies are deliberately updated through reviewed pull requests instead of unattended nightly upgrades.

## Automatic application updates

On a systemd VPS, the installer enables a timer that checks the installation's own `origin/main` every two minutes. This works identically for the original repository and forks—there are no deployment branches, GitHub secrets, webhooks, or inbound SSH access to configure.

When a new commit appears, the deployer:

1. accepts only a fast-forward update and refuses tracked local modifications;
2. preserves the current image for rollback;
3. builds the new image before replacing the running bot;
4. verifies the application and Telegram token with `getMe`;
5. starts the new container and checks that it remains stable;
6. restores the previous commit and image if any step fails.

The failed commit is not retried until a newer commit appears. Runtime configuration and persistent data remain on the VPS. For safer maintenance, protect `main` and require the included CI checks before merging pull requests.

Changes limited to documentation, repository metadata, development dependencies, or tests update the server checkout without rebuilding or restarting the running bot. Mixed commits and every unknown file are treated as runtime changes and receive the full deployment procedure.

Useful commands:

```bash
systemctl status linkdownloaderbotforgroups-deploy.timer
systemctl start linkdownloaderbotforgroups-deploy.service
tail -f /opt/linkdownloaderbot/logs/deploy-$(date -u +%Y-%m-%d).log
```

To stop automatic application updates without stopping the bot:

```bash
systemctl disable --now linkdownloaderbotforgroups-deploy.timer
```

## Development

Python 3.11 or newer is supported.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m pytest
```

The tests do not require a real Telegram token or live video sites.

## Security and privacy

- Secrets belong in `.env`, which is excluded from Git and Docker build context.
- URLs containing credentials or resolving to non-public IPv4/IPv6 addresses are rejected.
- The application runs as an unprivileged user after preparing its two writable directories.
- The container filesystem is read-only except for `data`, `logs`, and `/tmp`.
- Full URL query strings are not written to application logs.
- For an internet-facing deployment, also block private-network egress in the host firewall. Application-level DNS checks reduce SSRF risk but cannot replace network-level egress policy against every redirect or DNS-rebinding scenario.

Please report vulnerabilities according to [SECURITY.md](SECURITY.md).

## Legal notice

Use the bot only for content you are allowed to download and share. Site terms, copyright law and local regulations remain the operator's responsibility. This project does not bypass DRM.

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. The project is released under the [MIT License](LICENSE).
