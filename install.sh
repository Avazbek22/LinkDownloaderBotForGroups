#!/usr/bin/env bash
# One-time server setup that is safe to run again at any time. It installs
# Docker when it is missing, prepares .env (including the group access policy),
# starts the checked-out commit as a verified release, and enables automatic
# deployment for this bot only.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${ROOT_DIR:-$SCRIPT_DIR}"
LIBRARY="$SCRIPT_DIR/scripts/lib-production.sh"
[[ -f "$LIBRARY" ]] || {
  printf 'Run install.sh from a complete repository checkout.\n' >&2
  exit 1
}
# shellcheck source=scripts/lib-production.sh
source "$LIBRARY"

# Existing installations run as the Compose project "linkdownloaderbotforgroups"
# whatever the directory is called; keep that name unless one is configured.
DEFAULT_APP_SLUG="linkdownloaderbotforgroups"

install_prerequisites() {
  local -a packages=()
  if [[ "${INSTALL_SKIP_PREREQUISITES:-0}" == "1" ]]; then
    return 0
  fi
  [[ -r /etc/os-release ]] || die "Ubuntu 22.04 or 24.04 is required"
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || die "Ubuntu 22.04 or 24.04 is required"
  case "${VERSION_ID:-}" in
    22.04 | 24.04) ;;
    *) die "Supported Ubuntu versions are 22.04 and 24.04" ;;
  esac

  command_exists git || packages+=(git)
  command_exists docker || packages+=(docker.io)
  command_exists flock || packages+=(util-linux)
  command_exists curl || packages+=(curl)
  command_exists python3 || packages+=(python3)
  dpkg-query -W -f='${Status}' ca-certificates 2>/dev/null | grep -q 'ok installed' ||
    packages+=(ca-certificates)
  if ((${#packages[@]} > 0)); then
    apt-get update
    apt-get install -y --no-install-recommends "${packages[@]}"
  fi
  systemctl enable --now docker
  docker info >/dev/null 2>&1 || die "The Docker daemon is not running"

  if ! docker compose version >/dev/null 2>&1 && ! command_exists docker-compose; then
    apt-get install -y --no-install-recommends docker-compose-v2 ||
      apt-get install -y --no-install-recommends docker-compose-plugin ||
      apt-get install -y --no-install-recommends docker-compose
  fi
  compose version >/dev/null
}

validate_repository() {
  local branch
  [[ -d "$ROOT_DIR/.git" ]] ||
    die "Clone the repository with git before running install.sh"
  run_git remote get-url origin >/dev/null 2>&1 ||
    die "Git remote 'origin' is required for automatic deployment"
  branch="$(run_git branch --show-current)"
  [[ "$branch" == "$DEPLOY_BRANCH" ]] ||
    die "Check out $DEPLOY_BRANCH before running install.sh (now on '$branch')"
  [[ -z "$(run_git status --porcelain --untracked-files=no)" ]] ||
    die "Tracked files have local changes in $ROOT_DIR; commit or restore them first"
}

# Asks for the group access policy on the first run and keeps an existing
# policy on later runs instead of replacing it from the environment.
prepare_group_access() {
  local env_file="$1" created_env="$2" access_mode owner_username answer
  access_mode="$(env_value GROUP_ACCESS_MODE "$env_file")"
  if [[ -z "$access_mode" ]]; then
    access_mode="${GROUP_ACCESS_MODE:-}"
    if [[ -z "$access_mode" && -t 0 ]]; then
      while true; do
        if [[ "$created_env" == "1" ]]; then
          printf 'Require owner approval for new groups? [Y/n]: ' >&2
        else
          printf 'Enable owner approval for this existing installation? [y/N]: ' >&2
        fi
        read -r answer
        if [[ "$created_env" == "1" ]]; then
          case "${answer,,}" in
            "" | y | yes) access_mode="approval"; break ;;
            n | no) access_mode="open"; break ;;
            *) printf 'Please answer y or n.\n' >&2 ;;
          esac
        else
          case "${answer,,}" in
            y | yes) access_mode="approval"; break ;;
            "" | n | no) access_mode="open"; break ;;
            *) printf 'Please answer y or n.\n' >&2 ;;
          esac
        fi
      done
    fi
    [[ -n "$access_mode" ]] || access_mode="open"
  fi
  access_mode="${access_mode,,}"
  [[ "$access_mode" == "open" || "$access_mode" == "approval" ]] ||
    die "GROUP_ACCESS_MODE must be open or approval"
  set_env_value GROUP_ACCESS_MODE "$access_mode" "$env_file"

  if [[ "$access_mode" == "approval" ]]; then
    owner_username="$(env_value GROUP_OWNER_USERNAME "$env_file")"
    owner_username="${owner_username:-${GROUP_OWNER_USERNAME:-}}"
    if [[ -z "$owner_username" ]]; then
      [[ -t 0 ]] || die "GROUP_OWNER_USERNAME is required when GROUP_ACCESS_MODE=approval"
      printf 'Owner Telegram username (without @): ' >&2
      read -r owner_username
    fi
    owner_username="${owner_username#@}"
    owner_username="${owner_username,,}"
    [[ "$owner_username" =~ ^[a-z0-9_]{5,32}$ ]] ||
      die "Owner Telegram username has an invalid format"
    set_env_value GROUP_OWNER_USERNAME "$owner_username" "$env_file"
    if [[ -z "$(env_value PENDING_GROUP_TTL_HOURS "$env_file")" ]]; then
      set_env_value PENDING_GROUP_TTL_HOURS "168" "$env_file"
    fi
  fi
}

prepare_environment() {
  local env_file="$ROOT_DIR/.env" token created_env=0
  if [[ ! -f "$env_file" ]]; then
    install -m 600 "$ROOT_DIR/.env-example" "$env_file"
    created_env=1
  fi
  token="$(env_value BOT_TOKEN "$env_file")"
  if [[ -z "$token" ]]; then
    token="${BOT_TOKEN:-}"
    if [[ -z "$token" ]]; then
      [[ -t 0 ]] || die "BOT_TOKEN is required; pass it as an environment variable"
      printf 'Telegram BOT_TOKEN: ' >&2
      read -r -s token
      printf '\n' >&2
    fi
    [[ "$token" =~ ^$TOKEN_REGEX$ ]] || die "BOT_TOKEN has an invalid format"
    set_env_value BOT_TOKEN "$token" "$env_file"
  elif [[ ! "$token" =~ ^$TOKEN_REGEX$ ]]; then
    die "BOT_TOKEN in .env has an invalid format"
  fi

  prepare_group_access "$env_file" "$created_env"

  # Pin the name so that renaming the directory never orphans the bot. Older
  # installations named the Compose project through COMPOSE_PROJECT_NAME.
  local app_name
  if [[ -z "${APP_SLUG:-}" ]]; then
    APP_SLUG="$(env_value APP_SLUG "$env_file")"
  fi
  if [[ -z "$APP_SLUG" ]]; then
    APP_SLUG="$(env_value COMPOSE_PROJECT_NAME "$env_file")"
  fi
  if [[ -z "$APP_SLUG" ]]; then
    app_name="$(env_value APP_NAME "$env_file")"
    app_name="${app_name:-${APP_NAME:-}}"
    [[ -z "$app_name" ]] || APP_SLUG="$(slugify "$app_name")"
  fi
  APP_SLUG="${APP_SLUG:-$DEFAULT_APP_SLUG}"
  resolve_app_slug
  if [[ "$(env_value APP_SLUG "$env_file")" != "$APP_SLUG" ]]; then
    set_env_value APP_SLUG "$APP_SLUG" "$env_file"
  fi
  chmod 600 "$env_file"
  if [[ "$(id -u)" == "0" && -d "$ROOT_DIR/.git" ]]; then
    chown "$(stat -c '%u:%g' "$ROOT_DIR/.git")" "$env_file"
  fi
  # The container entrypoint gives data/ and logs/ to the bot user at start.
  mkdir -p "$ROOT_DIR/data" "$ROOT_DIR/logs"
}

# Older versions of these scripts kept rollback state in data/, extra image
# tags, and a separate yt-dlp updater timer, which the scheduled rebuild in
# deploy.conf now replaces. The running container is adopted by
# adopt_running_release.
migrate_legacy_state() {
  local tag unit legacy_failed="$ROOT_DIR/data/.failed-deploy-sha"
  if [[ -f "$legacy_failed" && ! -f "$(state_file failed-commit)" ]]; then
    tr -d '[:space:]' <"$legacy_failed" >"$(state_file failed-commit)"
  fi
  rm -f "$legacy_failed" "$ROOT_DIR/data/.rollback-commit"
  for tag in rollback install-rollback pre-manual-rollback; do
    remove_image_tag "$APP_SLUG:$tag"
  done
  unit="$APP_SLUG-yt-dlp-update"
  if [[ -f "$SYSTEMD_DIR/$unit.timer" || -f "$SYSTEMD_DIR/$unit.service" ]]; then
    "$SYSTEMCTL" disable --now "$unit.timer" >/dev/null 2>&1 || true
    rm -f "$SYSTEMD_DIR/$unit.timer" "$SYSTEMD_DIR/$unit.service"
    "$SYSTEMCTL" daemon-reload || true
    log "Removed the old $unit timer; deploy.conf schedules yt-dlp rebuilds now"
  fi
}

print_summary() {
  log "Installation complete: $APP_SLUG"
  cat <<SUMMARY

Every push to $DEPLOY_BRANCH is now deployed automatically once its CI checks pass.

  Status:    sudo bash scripts/status.sh
  Roll back: sudo bash scripts/rollback.sh
  Deploy:    sudo bash scripts/deploy.sh   (optional; the timer checks every two minutes)
  Logs:      $ROOT_DIR/logs/
SUMMARY
}

main() {
  install_prerequisites
  load_deploy_config
  validate_repository
  prepare_environment
  prepare_state_dir
  open_log
  acquire_lock 0 || die "Another deployment is running; try again in a minute"
  recover_interrupted_release
  adopt_running_release
  migrate_legacy_state
  release_commit "$(run_git rev-parse HEAD)" install
  install_units || die "Could not install the systemd timers"
  print_summary
}

# tests/shell/test-install.sh sources this file to exercise prepare_environment.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  if [[ "$(id -u)" != "0" ]]; then
    command_exists sudo || die "Run install.sh as root"
    exec sudo --preserve-env=BOT_TOKEN,APP_NAME,APP_SLUG,GROUP_ACCESS_MODE,GROUP_OWNER_USERNAME,INSTALL_SKIP_PREREQUISITES \
      bash "$SCRIPT_DIR/install.sh" "$@"
  fi
  main "$@"
fi
