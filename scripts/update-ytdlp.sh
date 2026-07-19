#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE_KEY="${SERVICE_KEY:-linkdownloaderbot}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-linkdownloaderbotforgroups}"
IMAGE_NAME="${IMAGE_NAME:-linkdownloaderbotforgroups:local}"
ROLLBACK_IMAGE="${IMAGE_NAME%:*}:rollback"

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
need_cmd() { command -v "$1" >/dev/null 2>&1; }

detect_compose() {
  if docker compose version >/dev/null 2>&1; then
    echo "docker compose"
  elif need_cmd docker-compose; then
    echo "docker-compose"
  else
    return 1
  fi
}

update_venv() {
  local python="$ROOT_DIR/.venv/bin/python"
  local pip="$ROOT_DIR/.venv/bin/pip"
  [[ -x "$python" && -x "$pip" ]] || return 1
  local before after
  before="$($python -m yt_dlp --version 2>/dev/null || true)"
  "$pip" install --upgrade 'yt-dlp[default,curl-cffi]'
  after="$($python -m yt_dlp --version)"
  log "venv yt-dlp: ${before:-unknown} -> $after"
  if [[ "$before" != "$after" ]] && systemctl list-unit-files 2>/dev/null | grep -q '^linkdownloaderbotforgroups.service'; then
    systemctl restart linkdownloaderbotforgroups.service
  fi
}

update_docker() {
  local compose before_id before_version after_version
  compose="$(detect_compose)"
  cd "$ROOT_DIR"
  before_id="$(docker image inspect "$IMAGE_NAME" --format '{{.Id}}' 2>/dev/null || true)"
  before_version="$($compose -p "$COMPOSE_PROJECT" run --rm --no-deps "$SERVICE_KEY" python -m yt_dlp --version 2>/dev/null || true)"
  if [[ -n "$before_id" ]]; then
    docker image tag "$IMAGE_NAME" "$ROLLBACK_IMAGE"
  fi

  $compose -p "$COMPOSE_PROJECT" build --pull \
    --build-arg "YTDLP_CACHEBUST=$(date -u '+%Y%m%dT%H%M%SZ')" "$SERVICE_KEY"
  $compose -p "$COMPOSE_PROJECT" run --rm --no-deps "$SERVICE_KEY" \
    python -c 'import main, telebot, yt_dlp; from app.settings import load_settings; s=load_settings(); telebot.TeleBot(s.token).get_me(); print(yt_dlp.version.__version__)' >/dev/null
  after_version="$($compose -p "$COMPOSE_PROJECT" run --rm --no-deps "$SERVICE_KEY" python -m yt_dlp --version)"

  if ! $compose -p "$COMPOSE_PROJECT" up -d --no-deps "$SERVICE_KEY"; then
    rollback_docker "$compose"
    return 1
  fi
  sleep 10
  local container_id running
  container_id="$($compose -p "$COMPOSE_PROJECT" ps -q "$SERVICE_KEY")"
  running="$(docker inspect --format '{{.State.Running}}' "$container_id" 2>/dev/null || true)"
  if [[ -z "$container_id" || "$running" != "true" ]]; then
    log "new container failed; rolling back"
    rollback_docker "$compose"
    return 1
  fi
  log "docker yt-dlp: ${before_version:-unknown} -> $after_version"
}

rollback_docker() {
  local compose="$1"
  if docker image inspect "$ROLLBACK_IMAGE" >/dev/null 2>&1; then
    docker image tag "$ROLLBACK_IMAGE" "$IMAGE_NAME"
    $compose -p "$COMPOSE_PROJECT" up -d --no-deps --force-recreate "$SERVICE_KEY"
  fi
}

docker_runtime_exists() {
  local compose
  compose="$(detect_compose)" || return 1
  cd "$ROOT_DIR"
  $compose -p "$COMPOSE_PROJECT" ps -a --services 2>/dev/null | grep -q "^${SERVICE_KEY}$"
}

main() {
  mkdir -p "$ROOT_DIR/logs"
  find "$ROOT_DIR/logs" -maxdepth 1 -type f -name 'updater-*.log' -mtime +60 -delete
  exec >>"$ROOT_DIR/logs/updater-$(date -u '+%Y-%m-%d').log" 2>&1
  cd "$ROOT_DIR"
  if docker_runtime_exists && update_docker; then
    exit 0
  fi
  if update_venv; then
    exit 0
  fi
  log "no supported runtime found"
  exit 1
}

main "$@"
