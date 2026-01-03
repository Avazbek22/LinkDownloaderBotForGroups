import os

# Required: Telegram bot token (from .env or environment variables)
token = (os.getenv("BOT_TOKEN") or "").strip()
if not token:
    raise RuntimeError("BOT_TOKEN is not set. Put it into .env or environment variables.")

# Optional logs chat id (disabled by default)
logs_raw = (os.getenv("LOGS_CHAT_ID") or "").strip()
if logs_raw:
    try:
        logs = int(logs_raw)
    except ValueError as ex:
        raise RuntimeError("LOGS_CHAT_ID must be an integer.") from ex
else:
    logs = None

# Max file size (bytes). Default: 50 MB.
max_filesize_raw = (os.getenv("MAX_FILESIZE") or "").strip()
if max_filesize_raw:
    try:
        max_filesize = int(max_filesize_raw)
    except ValueError as ex:
        raise RuntimeError("MAX_FILESIZE must be an integer (bytes).") from ex
else:
    max_filesize = 50 * 1024 * 1024

# Temp folder for downloads (can be overridden)
output_folder = (os.getenv("OUTPUT_FOLDER") or "/tmp/yt-dlp-telegram").strip() or "/tmp/yt-dlp-telegram"

# Optional: cookies file path (disabled by default)
cookies_file = (os.getenv("COOKIES_FILE") or "").strip() or None
