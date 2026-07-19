# Contributing

Thank you for helping improve Link Downloader Bot for Telegram Groups.

## Development workflow

1. Fork the repository and create a focused branch.
2. Install `requirements-dev.txt` in Python 3.11 or newer.
3. Preserve the quiet automatic-download behavior unless the change is explicitly discussed.
4. Add or update tests for behavior changes.
5. Run:

```bash
python -m ruff check .
python -m ruff format --check .
python -m pytest
docker build -t linkdownloaderbotforgroups:test .
```

Do not include bot tokens, cookies, downloaded media, logs, or real private chat data in issues or commits.

## Pull requests

Explain the user-visible effect, compatibility impact, tests performed, and any configuration changes. Keep unrelated refactoring separate. Security reports must follow `SECURITY.md` rather than a public issue.
