from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ci_checks_main_without_promoting_special_branches() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "branches: [main]" in workflow
    assert "production" not in workflow
    assert "contents: write" not in workflow


def test_installer_enables_main_updater_and_detects_a_fork() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "remote get-url origin" in installer
    assert "release_commit" in installer
    assert "install_units" in installer
    assert 'DEFAULT_APP_SLUG="linkdownloaderbotforgroups"' in installer
    assert "INSTALL_APP_UPDATER" not in installer

    deploy_conf = (ROOT / "deploy.conf").read_text(encoding="utf-8")
    assert "DEPLOY_BRANCH=main" in deploy_conf
    assert 'REBUILD_VERSION_CMD="python -m yt_dlp --version"' in deploy_conf


def test_installer_configures_optional_owner_approval_without_a_password() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    example = (ROOT / ".env-example").read_text(encoding="utf-8")

    assert "Require owner approval for new groups? [Y/n]" in installer
    assert "Enable owner approval for this existing installation? [y/N]" in installer
    assert "Owner Telegram username (without @)" in installer
    assert "GROUP_ACCESS_MODE" in installer
    assert "GROUP_OWNER_USERNAME" in installer
    assert "PENDING_GROUP_TTL_HOURS" in installer
    assert "GROUP_BOOTSTRAP_CHAT_IDS" in example
    assert "password" not in installer.lower()


def test_systemd_invokes_scripts_through_bash() -> None:
    systemd_dir = ROOT / "scripts/systemd"
    deploy_service = (systemd_dir / "telegram-bot-deploy.service").read_text(encoding="utf-8")
    rebuild_service = (systemd_dir / "telegram-bot-rebuild.service").read_text(encoding="utf-8")

    assert "ExecStart=/usr/bin/env bash" in deploy_service
    assert "scripts/deploy.sh" in deploy_service
    assert "ExecStart=/usr/bin/env bash" in rebuild_service
    assert "--rebuild" in rebuild_service


def test_compose_has_a_stable_default_project_name() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert compose.startswith("name: ${APP_SLUG:-linkdownloaderbotforgroups}\n")
    assert "image: ${APP_SLUG:-linkdownloaderbotforgroups}:${APP_IMAGE_TAG:-local}" in compose


def test_image_reports_health_from_polling() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert '["python", "-m", "app.healthcheck"]' in dockerfile
    assert "ARG REBUILD_STAMP" in dockerfile


def test_deployment_shell_tests_are_wired_into_ci() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert (ROOT / "tests/shell/test-deploy.sh").is_file()
    assert (ROOT / "tests/shell/test-install.sh").is_file()
    assert "for test_script in tests/shell/test-*.sh" in workflow
    assert 'bash "$test_script"' in workflow
