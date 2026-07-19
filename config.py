"""Backward-compatible configuration facade.

New code should import app.settings. Existing forks importing config continue to
receive the original attribute names.
"""

from app.settings import load_settings

_settings = load_settings()
token = _settings.token
logs = _settings.logs_chat_id
max_filesize = _settings.max_filesize
output_folder = str(_settings.output_dir)
cookies_file = str(_settings.cookies_file) if _settings.cookies_file else None
