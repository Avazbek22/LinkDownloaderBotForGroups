from __future__ import annotations

import logging

from app.i18n import tr
from app.logging_setup import configure_logging
from app.url_security import safe_error_for_log


def test_log_redacts_url_queries(tmp_path) -> None:
    logger = configure_logging(tmp_path, "INFO")
    logger.info("failed https://example.com/video?token=very-secret")
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = (tmp_path / "bot.log").read_text(encoding="utf-8")
    assert "very-secret" not in content
    assert "?<redacted>" in content
    for handler in list(logging.getLogger().handlers):
        handler.close()
        logging.getLogger().removeHandler(handler)


def test_translation_falls_back_to_english() -> None:
    assert "Only group administrators" in tr("unknown", "language_admin_only")


def test_error_summary_redacts_url_query_and_newlines() -> None:
    summary = safe_error_for_log(RuntimeError("failed\nhttps://example.com/video?token=secret"))

    assert summary == "failed https://example.com/video"
    assert "secret" not in summary
