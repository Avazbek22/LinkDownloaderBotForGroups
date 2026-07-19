#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DEPLOY_BRANCH="main"
SERVICE_KEY="${SERVICE_KEY:-linkdownloaderbot}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-linkdownloaderbotforgroups}"
IMAGE_NAME="${IMAGE_NAME:-linkdownloaderbotforgroups:local}"
ROLLBACK_IMAGE="${IMAGE_NAME%:*}:rollback"
LOCK_FILE="${LOCK_FILE:-/run/lock/linkdownloaderbotforgroups-update.lock}"
FAILED_SHA_FILE="$ROOT_DIR/data/.failed-deploy-sha"

old_commit=""
target_commit=""
deployment_started=0

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  else
    docker-compose "$@"
  fi
}

restore_previous_release() {
  local exit_code=$?
  [[ "$exit_code" -ne 0 ]] || exit_code=1
  trap - ERR INT TERM

  if [[ "$deployment_started" == "1" ]]; then
    log "deployment failed; restoring commit=$old_commit"
    if docker image inspect "$ROLLBACK_IMAGE" >/dev/null 2>&1; then
      docker image tag "$ROLLBACK_IMAGE" "$IMAGE_NAME" || true
    fi
    git -C "$ROOT_DIR" checkout -q -B "$DEPLOY_BRANCH" "$old_commit" || true
    compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" \
      up -d --no-deps --force-recreate "$SERVICE_KEY" || true
    printf '%s\n' "$target_commit" >"$FAILED_SHA_FILE"
  fi

  exit "$exit_code"
}

validate_checkout() {
  [[ -d "$ROOT_DIR/.git" ]] || { log "not a Git checkout: $ROOT_DIR"; return 1; }
  [[ -f "$ROOT_DIR/.env" ]] || { log "missing server configuration: $ROOT_DIR/.env"; return 1; }
  if [[ -n "$(git -C "$ROOT_DIR" status --porcelain --untracked-files=no)" ]]; then
    log "tracked server files have local changes; refusing automatic deployment"
    return 1
  fi
}

requires_container_update() {
  local path
  while IFS= read -r path; do
    case "$path" in
      README.md | LICENSE | CONTRIBUTING.md | CODE_OF_CONDUCT.md | SECURITY.md | requirements-dev.txt | \
        pyproject.toml | .github/* | tests/*)
        ;;
      *)
        return 0
        ;;
    esac
  done < <(git diff --name-only --diff-filter=ACDMRTUXB "$old_commit" "$target_commit")
  return 1
}

wait_until_running() {
  local container_id running restart_count attempt stable_checks=0
  for ((attempt = 1; attempt <= 15; attempt++)); do
    container_id="$(compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" \
      ps -q "$SERVICE_KEY")"
    if [[ -n "$container_id" ]]; then
      running="$(docker inspect --format '{{.State.Running}}' "$container_id" 2>/dev/null || true)"
      restart_count="$(docker inspect --format '{{.RestartCount}}' "$container_id" 2>/dev/null || true)"
      if [[ "$running" == "true" && "$restart_count" == "0" ]]; then
        stable_checks=$((stable_checks + 1))
        if [[ "$stable_checks" -ge 5 ]]; then
          return 0
        fi
      else
        stable_checks=0
      fi
    fi
    sleep 2
  done
  return 1
}

main() {
  mkdir -p "$ROOT_DIR/logs" "$ROOT_DIR/data" "$(dirname "$LOCK_FILE")"
  find "$ROOT_DIR/logs" -maxdepth 1 -type f -name 'deploy-*.log' -mtime +60 -delete
  exec >>"$ROOT_DIR/logs/deploy-$(date -u '+%Y-%m-%d').log" 2>&1

  command -v flock >/dev/null 2>&1 || { log "flock is required"; return 1; }
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    log "another updater is running; skipping"
    return 0
  fi

  validate_checkout
  cd "$ROOT_DIR"

  git fetch -q origin "+refs/heads/$DEPLOY_BRANCH:refs/remotes/origin/$DEPLOY_BRANCH"
  old_commit="$(git rev-parse HEAD)"
  target_commit="$(git rev-parse "refs/remotes/origin/$DEPLOY_BRANCH")"

  if [[ "$old_commit" == "$target_commit" ]]; then
    if [[ "$(git branch --show-current)" != "$DEPLOY_BRANCH" ]]; then
      git checkout -q -B "$DEPLOY_BRANCH" "$target_commit"
    fi
    return 0
  fi

  if [[ "${FORCE_DEPLOY:-0}" != "1" && -f "$FAILED_SHA_FILE" ]] \
      && [[ "$(tr -d '[:space:]' <"$FAILED_SHA_FILE")" == "$target_commit" ]]; then
    log "commit=$target_commit previously failed; waiting for a newer commit"
    return 0
  fi

  git merge-base --is-ancestor "$old_commit" "$target_commit" || {
    log "deployment ref is not a fast-forward from commit=$old_commit"
    return 1
  }

  if ! requires_container_update; then
    log "non-runtime update detected; container restart skipped commit=$target_commit"
    git checkout -q -B "$DEPLOY_BRANCH" "$target_commit"
    rm -f "$FAILED_SHA_FILE"
    return 0
  fi

  docker image inspect "$IMAGE_NAME" >/dev/null 2>&1 || {
    log "current image is unavailable: $IMAGE_NAME"
    return 1
  }
  docker image tag "$IMAGE_NAME" "$ROLLBACK_IMAGE"

  deployment_started=1
  trap restore_previous_release ERR INT TERM

  log "deploying commit=$target_commit"
  git checkout -q -B "$DEPLOY_BRANCH" "$target_commit"
  compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" build --pull "$SERVICE_KEY"
  compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" run --rm --no-deps "$SERVICE_KEY" \
    python -c 'import main, telebot; from app.settings import load_settings; s=load_settings(); telebot.TeleBot(s.token).get_me()'
  compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" \
    up -d --no-deps --force-recreate "$SERVICE_KEY"
  wait_until_running

  rm -f "$FAILED_SHA_FILE"
  deployment_started=0
  trap - ERR INT TERM
  log "deployment successful commit=$target_commit"
}

main "$@"
