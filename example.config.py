import os

# Example config (no secrets stored here).
# Put your real BOT_TOKEN into .env (or set it as an environment variable).

token = (os.getenv("BOT_TOKEN") or "").strip()
if not token:
    raise RuntimeError("BOT_TOKEN is not set. Put it into .env or environment variables.")

logs_raw = (os.getenv("LOGS_CHAT_ID") or "").strip()
logs = int(logs_raw) if logs_raw else None

max_filesize_raw = (os.getenv("MAX_FILESIZE") or "").strip()
max_filesize = int(max_filesize_raw) if max_filesize_raw else 50 * 1024 * 1024

output_folder = (os.getenv("OUTPUT_FOLDER") or "/tmp/yt-dlp-telegram").strip() or "/tmp/yt-dlp-telegram"

cookies_file = (os.getenv("COOKIES_FILE") or "").strip() or None
