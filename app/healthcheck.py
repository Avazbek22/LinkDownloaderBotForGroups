"""Docker healthcheck: healthy while Telegram answered getUpdates recently.

The bot refreshes HEALTH_MARKER after every successful getUpdates call, so a
revoked token, a network outage, or a second instance polling the same token
(HTTP 409) lets the marker age and the container turns unhealthy.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

HEALTH_MARKER = Path("/tmp/linkdownloaderbot.healthy")
# Long polling returns at least every 30 seconds while the bot is healthy.
MAX_AGE_SECONDS = 120


def marker_is_fresh(marker: Path, max_age_seconds: int, now: float | None = None) -> bool:
    try:
        modified = marker.stat().st_mtime
    except OSError:
        return False
    age = (time.time() if now is None else now) - modified
    return 0 <= age < max_age_seconds


def main() -> int:
    marker = Path(os.getenv("HEALTH_MARKER") or HEALTH_MARKER)
    return 0 if marker_is_fresh(marker, MAX_AGE_SECONDS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
