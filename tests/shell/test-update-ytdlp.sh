#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

make_fake_commands() {
  local fake_bin="$1"
  mkdir -p "$fake_bin"

  cat >"$fake_bin/docker" <<'FAKE_DOCKER'
#!/usr/bin/env bash
set -euo pipefail
printf 'docker %s\n' "$*" >>"$FAKE_COMMAND_LOG"

image_id() {
  if [[ "$1" == *:rollback ]]; then
    cat "$FAKE_STATE_DIR/rollback_id"
  else
    cat "$FAKE_STATE_DIR/main_id"
  fi
}

if [[ "${1:-}" == "compose" ]]; then
  case "$*" in
    "compose version") exit 0 ;;
    *" ps -a --services") echo linkdownloaderbot ;;
    *" build "*)
      [[ "${FAKE_FAIL_BUILD:-0}" != "1" ]] || exit 1
      printf '%s\n' candidate-image >"$FAKE_STATE_DIR/main_id"
      printf '%s\n' 1 >"$FAKE_STATE_DIR/built"
      ;;
    *" run "*"python -m yt_dlp --version")
      if [[ "$(<"$FAKE_STATE_DIR/built")" == "1" ]]; then
        printf '%s\n' "$FAKE_AFTER_VERSION"
      else
        printf '%s\n' "$FAKE_BEFORE_VERSION"
      fi
      ;;
    *" run "*"python -c "*)
      [[ "${FAKE_FAIL_SMOKE:-0}" != "1" ]] || exit 1
      printf '%s\n' "$FAKE_AFTER_VERSION"
      ;;
    *" up -d "*)
      [[ "${FAKE_FAIL_UP:-0}" != "1" ]] || exit 1
      if [[ "${FAKE_KEEP_OLD_CONTAINER:-0}" != "1" ]]; then
        cat "$FAKE_STATE_DIR/main_id" >"$FAKE_STATE_DIR/running_id"
      fi
      ;;
    *" ps -q "*) echo fake-container ;;
  esac
  exit 0
fi

if [[ "${1:-}" == "image" && "${2:-}" == "inspect" ]]; then
  if [[ "${4:-}" == "--format" ]]; then
    image_id "$3"
  elif [[ "$3" == *:rollback ]]; then
    [[ -s "$FAKE_STATE_DIR/rollback_id" ]]
  else
    [[ -s "$FAKE_STATE_DIR/main_id" ]]
  fi
  exit $?
fi

if [[ "${1:-}" == "image" && "${2:-}" == "tag" ]]; then
  if [[ "$3" == *:rollback && "${FAKE_FAIL_ROLLBACK_TAG:-0}" == "1" ]]; then
    exit 1
  fi
  source_id="$(image_id "$3")"
  if [[ "$4" == *:rollback ]]; then
    printf '%s\n' "$source_id" >"$FAKE_STATE_DIR/rollback_id"
  else
    printf '%s\n' "$source_id" >"$FAKE_STATE_DIR/main_id"
  fi
  exit 0
fi

if [[ "${1:-}" == "image" && "${2:-}" == "rm" ]]; then
  exit 0
fi

if [[ "${1:-}" == "inspect" && "${3:-}" == "{{.State.Running}}" ]]; then
  if [[ "${FAKE_RUNNING_FALSE:-0}" == "1" ]]; then
    echo false
  else
    echo true
  fi
  exit 0
fi

if [[ "${1:-}" == "inspect" && "${3:-}" == "{{.Image}}" ]]; then
  cat "$FAKE_STATE_DIR/running_id"
  exit 0
fi

if [[ "${1:-}" == "inspect" && "${3:-}" == "{{.RestartCount}}" ]]; then
  echo "${FAKE_RESTART_COUNT:-0}"
  exit 0
fi

exit 0
FAKE_DOCKER

  cat >"$fake_bin/flock" <<'FAKE_FLOCK'
#!/usr/bin/env bash
exit 0
FAKE_FLOCK

  cat >"$fake_bin/sleep" <<'FAKE_SLEEP'
#!/usr/bin/env bash
exit 0
FAKE_SLEEP

  chmod 755 "$fake_bin/docker" "$fake_bin/flock" "$fake_bin/sleep"
}

prepare_case() {
  local case_root="$1"
  mkdir -p "$case_root/.git" "$case_root/logs" "$case_root/bin" "$case_root/lock"
  : >"$case_root/.env"
  : >"$case_root/docker-compose.yml"
  : >"$case_root/commands.log"
  : >"$case_root/rollback_id"
  printf '%s\n' old-image >"$case_root/main_id"
  printf '%s\n' old-image >"$case_root/running_id"
  printf '%s\n' 0 >"$case_root/built"
  make_fake_commands "$case_root/bin"
}

run_updater() {
  local case_root="$1"
  shift
  env \
    PATH="$case_root/bin:$PATH" \
    ROOT_DIR="$case_root" \
    LOCK_FILE="$case_root/lock/update.lock" \
    FAKE_STATE_DIR="$case_root" \
    FAKE_COMMAND_LOG="$case_root/commands.log" \
    FAKE_BEFORE_VERSION=2026.01.01 \
    FAKE_AFTER_VERSION=2026.02.01 \
    "$@" \
    bash "$REPOSITORY_ROOT/scripts/update-ytdlp.sh"
}

unchanged_root="$TEST_ROOT/unchanged"
prepare_case "$unchanged_root"
run_updater "$unchanged_root" FAKE_AFTER_VERSION=2026.01.01
[[ "$(<"$unchanged_root/main_id")" == "old-image" ]]
grep -q 'docker compose .* build --build-arg' "$unchanged_root/commands.log"
if grep -q 'build --pull' "$unchanged_root/commands.log"; then
  echo "yt-dlp-only update unexpectedly pulled the base image" >&2
  exit 1
fi
if grep -q 'compose .* up -d' "$unchanged_root/commands.log"; then
  echo "unchanged yt-dlp version unexpectedly restarted the container" >&2
  exit 1
fi
grep -q 'docker image rm candidate-image' "$unchanged_root/commands.log"
grep -q 'container restart skipped' "$unchanged_root/logs/updater-"*.log

success_root="$TEST_ROOT/success"
prepare_case "$success_root"
run_updater "$success_root"
[[ "$(<"$success_root/main_id")" == "candidate-image" ]]
grep -q 'docker compose .* run .* python -c ' "$success_root/commands.log"
grep -q 'docker compose .* up -d --no-deps linkdownloaderbot' "$success_root/commands.log"
[[ "$(grep -c '{{.State.Running}}' "$success_root/commands.log")" -ge 5 ]]
[[ "$(grep -c '{{.RestartCount}}' "$success_root/commands.log")" -ge 5 ]]
[[ "$(grep -c '{{.Image}}' "$success_root/commands.log")" -ge 5 ]]
grep -q 'docker yt-dlp: 2026.01.01 -> 2026.02.01' "$success_root/logs/updater-"*.log

smoke_failure_root="$TEST_ROOT/smoke-failure"
prepare_case "$smoke_failure_root"
if run_updater "$smoke_failure_root" FAKE_FAIL_SMOKE=1; then
  echo "expected the simulated smoke check to fail" >&2
  exit 1
fi
[[ "$(<"$smoke_failure_root/main_id")" == "old-image" ]]
if grep -q 'compose .* up -d' "$smoke_failure_root/commands.log"; then
  echo "failed candidate unexpectedly replaced the running container" >&2
  exit 1
fi
grep -q 'docker image rm candidate-image' "$smoke_failure_root/commands.log"
grep -q 'failed its smoke check' "$smoke_failure_root/logs/updater-"*.log

restart_failure_root="$TEST_ROOT/restart-failure"
prepare_case "$restart_failure_root"
if run_updater "$restart_failure_root" FAKE_RESTART_COUNT=1; then
  echo "expected an unstable container to fail" >&2
  exit 1
fi
[[ "$(<"$restart_failure_root/main_id")" == "old-image" ]]
grep -q 'compose .* up -d --no-deps --force-recreate linkdownloaderbot' "$restart_failure_root/commands.log"
grep -q 'docker image rm candidate-image' "$restart_failure_root/commands.log"
grep -q 'new container failed; rolling back' "$restart_failure_root/logs/updater-"*.log

wrong_image_root="$TEST_ROOT/wrong-image"
prepare_case "$wrong_image_root"
if run_updater "$wrong_image_root" FAKE_KEEP_OLD_CONTAINER=1; then
  echo "expected a container using the old image to fail" >&2
  exit 1
fi
[[ "$(<"$wrong_image_root/main_id")" == "old-image" ]]
grep -q 'compose .* up -d --no-deps --force-recreate linkdownloaderbot' "$wrong_image_root/commands.log"
grep -q 'new container failed; rolling back' "$wrong_image_root/logs/updater-"*.log

rollback_failure_root="$TEST_ROOT/rollback-failure"
prepare_case "$rollback_failure_root"
if run_updater "$rollback_failure_root" FAKE_FAIL_SMOKE=1 FAKE_FAIL_ROLLBACK_TAG=1; then
  echo "expected the simulated rollback to fail" >&2
  exit 1
fi
[[ "$(<"$rollback_failure_root/main_id")" == "candidate-image" ]]
grep -q 'rollback failed; manual recovery may be required' "$rollback_failure_root/logs/updater-"*.log
if grep -q 'docker image rm candidate-image' "$rollback_failure_root/commands.log"; then
  echo "failed rollback unexpectedly removed its recovery candidate" >&2
  exit 1
fi

build_failure_root="$TEST_ROOT/build-failure"
prepare_case "$build_failure_root"
if run_updater "$build_failure_root" FAKE_FAIL_BUILD=1; then
  echo "expected the simulated build to fail" >&2
  exit 1
fi
[[ "$(<"$build_failure_root/main_id")" == "old-image" ]]
grep -q 'Docker yt-dlp image build failed' "$build_failure_root/logs/updater-"*.log
