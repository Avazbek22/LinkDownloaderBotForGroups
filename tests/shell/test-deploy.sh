#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

make_fake_commands() {
  local fake_bin="$1"
  mkdir -p "$fake_bin"

  cat >"$fake_bin/git" <<'FAKE_GIT'
#!/usr/bin/env bash
set -euo pipefail
printf 'git %s\n' "$*" >>"$FAKE_COMMAND_LOG"
case "$*" in
  *"status --porcelain"*) exit 0 ;;
  *"rev-parse HEAD"*) cat "$FAKE_STATE_DIR/head" ;;
  *"rev-parse refs/remotes/origin/production-ready"*) cat "$FAKE_STATE_DIR/target" ;;
  *"branch --show-current"*) echo main ;;
  *"merge-base --is-ancestor"*) exit 0 ;;
  *"checkout -q -B"*) printf '%s\n' "${!#}" >"$FAKE_STATE_DIR/head" ;;
esac
FAKE_GIT

  cat >"$fake_bin/docker" <<'FAKE_DOCKER'
#!/usr/bin/env bash
set -euo pipefail
printf 'docker %s\n' "$*" >>"$FAKE_COMMAND_LOG"
case "$*" in
  "compose version") exit 0 ;;
  "image inspect "*) exit 0 ;;
  "image tag "*) exit 0 ;;
  *" build "*) [[ "${FAKE_FAIL_BUILD:-0}" != "1" ]] ;;
  *" ps -q "*) echo fake-container ;;
  *"{{.State.Running}}"*) echo true ;;
  *"{{.RestartCount}}"*) echo 0 ;;
esac
FAKE_DOCKER

  cat >"$fake_bin/flock" <<'FAKE_FLOCK'
#!/usr/bin/env bash
exit 0
FAKE_FLOCK

  cat >"$fake_bin/sleep" <<'FAKE_SLEEP'
#!/usr/bin/env bash
exit 0
FAKE_SLEEP

  chmod 755 "$fake_bin/git" "$fake_bin/docker" "$fake_bin/flock" "$fake_bin/sleep"
}

prepare_case() {
  local case_root="$1"
  mkdir -p "$case_root/.git" "$case_root/data" "$case_root/logs" "$case_root/bin" "$case_root/lock"
  : >"$case_root/.env"
  : >"$case_root/docker-compose.yml"
  printf '%s\n' old-commit >"$case_root/head"
  printf '%s\n' new-commit >"$case_root/target"
  : >"$case_root/commands.log"
  make_fake_commands "$case_root/bin"
}

run_deployer() {
  local case_root="$1"
  shift
  env \
    PATH="$case_root/bin:$PATH" \
    ROOT_DIR="$case_root" \
    LOCK_FILE="$case_root/lock/update.lock" \
    FAKE_STATE_DIR="$case_root" \
    FAKE_COMMAND_LOG="$case_root/commands.log" \
    "$@" \
    bash "$REPOSITORY_ROOT/scripts/deploy-production.sh"
}

success_root="$TEST_ROOT/success"
prepare_case "$success_root"
run_deployer "$success_root"
[[ "$(<"$success_root/head")" == "new-commit" ]]
grep -q 'deployment successful commit=new-commit' "$success_root/logs/deploy-"*.log
grep -q 'docker compose .* build --pull linkdownloaderbot' "$success_root/commands.log"

failure_root="$TEST_ROOT/failure"
prepare_case "$failure_root"
if run_deployer "$failure_root" FAKE_FAIL_BUILD=1; then
  echo "expected the simulated build to fail" >&2
  exit 1
fi
[[ "$(<"$failure_root/head")" == "old-commit" ]]
[[ "$(<"$failure_root/data/.failed-deploy-sha")" == "new-commit" ]]
grep -q 'docker image tag linkdownloaderbotforgroups:rollback linkdownloaderbotforgroups:local' \
  "$failure_root/commands.log"
grep -q 'docker compose .* up -d --no-deps --force-recreate linkdownloaderbot' \
  "$failure_root/commands.log"
