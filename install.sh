#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPOSITORY="https://github.com/Avazbek22/LinkDownloaderBotForGroups"
BRANCH="main"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-linkdownloaderbotforgroups}"
SERVICE_KEY="linkdownloaderbot"

if [[ -d "$SCRIPT_DIR/.git" ]]; then
  INSTALL_DIR="${INSTALL_DIR:-$SCRIPT_DIR}"
  REPOSITORY="${REPOSITORY:-$(git -C "$SCRIPT_DIR" remote get-url origin 2>/dev/null || true)}"
else
  INSTALL_DIR="${INSTALL_DIR:-$PWD/LinkDownloaderBotForGroups}"
  REPOSITORY="${REPOSITORY:-$DEFAULT_REPOSITORY}"
fi

[[ -n "$REPOSITORY" ]] || REPOSITORY="$DEFAULT_REPOSITORY"

info() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
ok() { printf '\033[32m✓\033[0m %s\n' "$*"; }
die() { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1; }

as_root() {
  if [[ "$(id -u)" == "0" ]]; then "$@"; else need sudo || die "sudo is required"; sudo "$@"; fi
}

compose() {
  if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi
}

install_prerequisites() {
  info "Checking prerequisites"
  if ! need git || ! need docker || ! need flock; then
    need apt-get || die "Install Git and Docker manually on this operating system"
    as_root apt-get update -y
    need git || as_root apt-get install -y git ca-certificates
    need docker || as_root apt-get install -y docker.io
    need flock || as_root apt-get install -y util-linux
  fi
  need docker || die "Docker is unavailable"
  if ! docker compose version >/dev/null 2>&1 && ! need docker-compose; then
    as_root apt-get install -y docker-compose
  fi
  (docker compose version >/dev/null 2>&1 || need docker-compose) || die "Docker Compose is unavailable"
  ok "Prerequisites are ready"
}

prepare_repository() {
  info "Preparing repository"
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    if [[ -n "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=no)" ]]; then
      die "The existing repository has local changes: $INSTALL_DIR"
    fi
    git -C "$INSTALL_DIR" fetch origin "$BRANCH"
    git -C "$INSTALL_DIR" checkout "$BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
  else
    git clone --branch "$BRANCH" "$REPOSITORY" "$INSTALL_DIR"
  fi
  ok "Repository is ready at $INSTALL_DIR"
}

prepare_environment() {
  info "Preparing configuration"
  local env_file="$INSTALL_DIR/.env"
  if [[ ! -f "$env_file" ]]; then
    cp "$INSTALL_DIR/.env-example" "$env_file"
  fi
  if ! grep -Eq '^BOT_TOKEN=.+$' "$env_file"; then
    local token="${BOT_TOKEN:-}"
    if [[ -z "$token" ]]; then
      printf 'Telegram BOT_TOKEN: ' >&2
      read -r -s token
      printf '\n' >&2
    fi
    [[ "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]] || die "BOT_TOKEN has an invalid format"
    local temporary="$env_file.tmp"
    awk -v token="$token" 'BEGIN{done=0} /^BOT_TOKEN=/{print "BOT_TOKEN=" token; done=1; next} {print} END{if(!done) print "BOT_TOKEN=" token}' \
      "$env_file" >"$temporary"
    mv "$temporary" "$env_file"
    chmod 600 "$env_file"
  fi
  mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/logs"
  ok "Configuration is ready"
}

start_bot() {
  info "Building and starting the bot"
  cd "$INSTALL_DIR"
  compose -p "$COMPOSE_PROJECT" up -d --build
  compose -p "$COMPOSE_PROJECT" ps
  ok "Bot started"
}

install_updater() {
  [[ "${INSTALL_UPDATER:-1}" == "1" ]] || return 0
  need systemctl || return 0
  [[ -d /run/systemd/system ]] || return 0
  info "Installing nightly yt-dlp updater"
  local service="/etc/systemd/system/linkdownloaderbotforgroups-yt-dlp-update.service"
  local timer="/etc/systemd/system/linkdownloaderbotforgroups-yt-dlp-update.timer"
  sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
      -e "s|__COMPOSE_PROJECT__|$COMPOSE_PROJECT|g" \
      -e "s|__SERVICE_KEY__|$SERVICE_KEY|g" \
      "$INSTALL_DIR/scripts/systemd/linkdownloaderbotforgroups-yt-dlp-update.service" | as_root tee "$service" >/dev/null
  as_root cp "$INSTALL_DIR/scripts/systemd/linkdownloaderbotforgroups-yt-dlp-update.timer" "$timer"
  as_root systemctl daemon-reload
  as_root systemctl enable --now linkdownloaderbotforgroups-yt-dlp-update.timer
  ok "Nightly updater installed"
}

install_app_updater() {
  need systemctl || return 0
  [[ -d /run/systemd/system ]] || return 0
  info "Installing automatic application updates"
  local service="/etc/systemd/system/linkdownloaderbotforgroups-deploy.service"
  local timer="/etc/systemd/system/linkdownloaderbotforgroups-deploy.timer"
  sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
      -e "s|__COMPOSE_PROJECT__|$COMPOSE_PROJECT|g" \
      -e "s|__SERVICE_KEY__|$SERVICE_KEY|g" \
      "$INSTALL_DIR/scripts/systemd/linkdownloaderbotforgroups-deploy.service" | as_root tee "$service" >/dev/null
  as_root cp "$INSTALL_DIR/scripts/systemd/linkdownloaderbotforgroups-deploy.timer" "$timer"
  as_root systemctl daemon-reload
  as_root systemctl enable --now linkdownloaderbotforgroups-deploy.timer
  ok "Automatic updates installed for origin/main"
}

main() {
  install_prerequisites
  prepare_repository
  prepare_environment
  start_bot
  install_updater
  install_app_updater
  printf '\nLogs: cd %q && docker compose -p %q logs -f --tail=200\n' "$INSTALL_DIR" "$COMPOSE_PROJECT"
}

main "$@"
