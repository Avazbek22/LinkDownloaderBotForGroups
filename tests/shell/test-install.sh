#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091 # install.sh is exercised directly below
source "$REPOSITORY_ROOT/install.sh"

test_root="$(mktemp -d)"
case "$test_root" in
  /tmp/* | /var/tmp/*) ;;
  *) printf 'Unexpected temporary path: %s\n' "$test_root" >&2; exit 1 ;;
esac
trap 'rm -rf -- "$test_root"' EXIT

new_install_dir() {
  ROOT_DIR="$test_root/$1"
  mkdir -p "$ROOT_DIR"
  cp "$REPOSITORY_ROOT/.env-example" "$ROOT_DIR/.env-example"
  unset APP_SLUG
}

new_install_dir approval
export BOT_TOKEN="123456:abcdefghijklmnopqrstuvwxyz"
export GROUP_ACCESS_MODE="approval"
export GROUP_OWNER_USERNAME="@Owner_Name"
prepare_environment </dev/null >/dev/null

grep -qx 'BOT_TOKEN=123456:abcdefghijklmnopqrstuvwxyz' "$ROOT_DIR/.env"
grep -qx 'GROUP_ACCESS_MODE=approval' "$ROOT_DIR/.env"
grep -qx 'GROUP_OWNER_USERNAME=owner_name' "$ROOT_DIR/.env"
grep -qx 'PENDING_GROUP_TTL_HOURS=168' "$ROOT_DIR/.env"
grep -qx 'APP_SLUG=linkdownloaderbotforgroups' "$ROOT_DIR/.env"
[[ "$(stat -c '%a' "$ROOT_DIR/.env")" == "600" ]]

# A later installer run must preserve the existing policy instead of silently
# replacing it with values from the invoking environment.
export GROUP_ACCESS_MODE="open"
export GROUP_OWNER_USERNAME="another_owner"
unset APP_SLUG
prepare_environment </dev/null >/dev/null
grep -qx 'GROUP_ACCESS_MODE=approval' "$ROOT_DIR/.env"
grep -qx 'GROUP_OWNER_USERNAME=owner_name' "$ROOT_DIR/.env"

new_install_dir open
unset GROUP_ACCESS_MODE GROUP_OWNER_USERNAME
prepare_environment </dev/null >/dev/null
grep -qx 'GROUP_ACCESS_MODE=open' "$ROOT_DIR/.env"
if grep -q '^GROUP_OWNER_USERNAME=' "$ROOT_DIR/.env"; then
  printf 'Open mode unexpectedly configured an owner\n' >&2
  exit 1
fi

new_install_dir existing
printf 'BOT_TOKEN=123456:abcdefghijklmnopqrstuvwxyz\n' >"$ROOT_DIR/.env"
prepare_environment </dev/null >/dev/null
grep -qx 'GROUP_ACCESS_MODE=open' "$ROOT_DIR/.env"

# Older installations named the Compose project through COMPOSE_PROJECT_NAME.
new_install_dir legacy-name
printf 'BOT_TOKEN=123456:abcdefghijklmnopqrstuvwxyz\nCOMPOSE_PROJECT_NAME=my-links-bot\n' \
  >"$ROOT_DIR/.env"
prepare_environment </dev/null >/dev/null
grep -qx 'APP_SLUG=my-links-bot' "$ROOT_DIR/.env"

printf 'Installer environment tests passed.\n'
