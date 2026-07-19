from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_production_is_promoted_only_after_ci_jobs() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "branches: [main, production]" in workflow
    assert "needs: [test, docker]" in workflow
    assert "github.ref == 'refs/heads/production'" in workflow
    assert "refs/heads/production-ready" in workflow
    assert "contents: write" in workflow


def test_application_updater_is_opt_in() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "INSTALL_APP_UPDATER:-0" in installer
    assert "linkdownloaderbotforgroups-deploy.timer" in installer
    assert 'DEPLOY_BRANCH="${DEPLOY_BRANCH:-production-ready}"' in installer


def test_systemd_invokes_scripts_through_bash() -> None:
    systemd_dir = ROOT / "scripts/systemd"
    deploy_service = (systemd_dir / "linkdownloaderbotforgroups-deploy.service").read_text(encoding="utf-8")
    ytdlp_service = (systemd_dir / "linkdownloaderbotforgroups-yt-dlp-update.service").read_text(encoding="utf-8")

    assert "ExecStart=/usr/bin/env bash" in deploy_service
    assert "ExecStart=/usr/bin/env bash" in ytdlp_service


def test_compose_has_a_stable_default_project_name() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert compose.startswith("name: ${COMPOSE_PROJECT_NAME:-linkdownloaderbotforgroups}\n")
