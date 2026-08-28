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
    assert 'BRANCH="main"' in installer
    assert "linkdownloaderbotforgroups-deploy.timer" in installer
    assert "INSTALL_APP_UPDATER" not in installer

    deployer = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    assert 'DEPLOY_BRANCH="main"' in deployer
    assert "production-ready" not in deployer


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
    deploy_service = (systemd_dir / "linkdownloaderbotforgroups-deploy.service").read_text(encoding="utf-8")
    ytdlp_service = (systemd_dir / "linkdownloaderbotforgroups-yt-dlp-update.service").read_text(encoding="utf-8")

    assert "ExecStart=/usr/bin/env bash" in deploy_service
    assert "scripts/deploy.sh" in deploy_service
    assert "ExecStart=/usr/bin/env bash" in ytdlp_service


def test_compose_has_a_stable_default_project_name() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert compose.startswith("name: ${COMPOSE_PROJECT_NAME:-linkdownloaderbotforgroups}\n")


def test_ytdlp_updater_shell_test_is_wired_into_ci() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert (ROOT / "tests/shell/test-update-ytdlp.sh").is_file()
    assert "for test_script in tests/shell/test-*.sh" in workflow
    assert 'bash "$test_script"' in workflow
