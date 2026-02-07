#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE_KEY="${SERVICE_KEY:-linkdownloaderbot}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-linkdownloaderbotforgroups}"

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

need_cmd() { command -v "$1" >/dev/null 2>&1; }

detect_compose() {
  if need_cmd docker && docker compose version >/dev/null 2>&1; then
    echo "docker compose"
    return 0
  fi
  if need_cmd docker-compose; then
    echo "docker-compose"
    return 0
  fi
  return 1
}

log_runtime() {
  local py="$1"
  local node_line="node:missing"
  if need_cmd node; then
    node_line="node:$(node --version 2>/dev/null || echo unknown)"
  fi
  local targets_line
  targets_line="$("$py" -m yt_dlp --list-impersonate-targets 2>/dev/null | tr '\n' ' ' || true)"
  if [[ -n "$targets_line" ]]; then
    log "$node_line | impersonate-targets: $targets_line"
  else
    log "$node_line | impersonate-targets: unavailable"
  fi
}

update_venv_mode() {
  local venv_python="$ROOT_DIR/.venv/bin/python"
  local venv_pip="$ROOT_DIR/.venv/bin/pip"
  [[ -x "$venv_python" && -x "$venv_pip" ]] || return 1

  local before after
  before="$("$venv_python" -m yt_dlp --version 2>/dev/null || true)"
  "$venv_pip" install --upgrade 'yt-dlp[default,curl-cffi]' >/dev/null
  after="$("$venv_python" -m yt_dlp --version 2>/dev/null || true)"
  log_runtime "$venv_python"

  if [[ -n "$before" && -n "$after" && "$before" != "$after" ]]; then
    log "yt-dlp changed in venv: $before -> $after"
    if need_cmd systemctl && systemctl list-unit-files | grep -q '^linkdownloaderbotforgroups.service'; then
      systemctl restart linkdownloaderbotforgroups.service || true
      log "restarted linkdownloaderbotforgroups.service"
    fi
  else
    log "yt-dlp unchanged in venv: ${after:-unknown}"
  fi
  return 0
}

update_docker_mode() {
  local compose_cmd
  compose_cmd="$(detect_compose)" || return 1

  local before after
  before="$($compose_cmd -p "$COMPOSE_PROJECT" exec -T "$SERVICE_KEY" python -m yt_dlp --version 2>/dev/null || true)"
  $compose_cmd -p "$COMPOSE_PROJECT" build "$SERVICE_KEY" >/dev/null
  after="$($compose_cmd -p "$COMPOSE_PROJECT" run --rm --no-deps "$SERVICE_KEY" python -m yt_dlp --version 2>/dev/null || true)"

  if [[ -n "$before" && -n "$after" && "$before" != "$after" ]]; then
    $compose_cmd -p "$COMPOSE_PROJECT" up -d --no-deps "$SERVICE_KEY" >/dev/null
    log "yt-dlp changed in docker: $before -> $after (service restarted)"
  else
    log "yt-dlp unchanged in docker: ${after:-unknown}"
  fi

  local node_line="node:unknown"
  node_line="$($compose_cmd -p "$COMPOSE_PROJECT" run --rm --no-deps "$SERVICE_KEY" sh -lc 'node --version 2>/dev/null || echo missing' 2>/dev/null || true)"
  local targets_line
  targets_line="$($compose_cmd -p "$COMPOSE_PROJECT" run --rm --no-deps "$SERVICE_KEY" python -m yt_dlp --list-impersonate-targets 2>/dev/null | tr '\n' ' ' || true)"
  log "container-node:${node_line:-missing} | impersonate-targets: ${targets_line:-unavailable}"
  return 0
}

main() {
  cd "$ROOT_DIR"
  if update_venv_mode; then
    exit 0
  fi
  if update_docker_mode; then
    exit 0
  fi
  log "No supported runtime found (.venv or docker compose). Nothing to update."
}

main "$@"
