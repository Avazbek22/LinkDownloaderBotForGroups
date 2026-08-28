#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=../../install.sh
source "$ROOT_DIR/install.sh"

test_root="$(mktemp -d)"
case "$test_root" in
  /tmp/*|/var/tmp/*) ;;
  *) printf 'Unexpected temporary path: %s\n' "$test_root" >&2; exit 1 ;;
esac
trap 'rm -rf -- "$test_root"' EXIT

INSTALL_DIR="$test_root/approval"
mkdir -p "$INSTALL_DIR"
cp "$ROOT_DIR/.env-example" "$INSTALL_DIR/.env-example"
BOT_TOKEN="123456:abcdefghijklmnopqrstuvwxyz"
GROUP_ACCESS_MODE="approval"
GROUP_OWNER_USERNAME="@Owner_Name"
prepare_environment </dev/null >/dev/null

grep -qx 'BOT_TOKEN=123456:abcdefghijklmnopqrstuvwxyz' "$INSTALL_DIR/.env"
grep -qx 'GROUP_ACCESS_MODE=approval' "$INSTALL_DIR/.env"
grep -qx 'GROUP_OWNER_USERNAME=owner_name' "$INSTALL_DIR/.env"
grep -qx 'PENDING_GROUP_TTL_HOURS=168' "$INSTALL_DIR/.env"
[[ "$(stat -c '%a' "$INSTALL_DIR/.env")" == "600" ]]

# A later installer run must preserve the existing policy instead of silently
# replacing it with values from the invoking environment.
GROUP_ACCESS_MODE="open"
GROUP_OWNER_USERNAME="another_owner"
prepare_environment </dev/null >/dev/null
grep -qx 'GROUP_ACCESS_MODE=approval' "$INSTALL_DIR/.env"
grep -qx 'GROUP_OWNER_USERNAME=owner_name' "$INSTALL_DIR/.env"

INSTALL_DIR="$test_root/open"
mkdir -p "$INSTALL_DIR"
cp "$ROOT_DIR/.env-example" "$INSTALL_DIR/.env-example"
unset GROUP_ACCESS_MODE GROUP_OWNER_USERNAME
prepare_environment </dev/null >/dev/null
grep -qx 'GROUP_ACCESS_MODE=open' "$INSTALL_DIR/.env"
if grep -q '^GROUP_OWNER_USERNAME=' "$INSTALL_DIR/.env"; then
  printf 'Open mode unexpectedly configured an owner\n' >&2
  exit 1
fi

INSTALL_DIR="$test_root/existing"
mkdir -p "$INSTALL_DIR"
cp "$ROOT_DIR/.env-example" "$INSTALL_DIR/.env-example"
printf 'BOT_TOKEN=123456:abcdefghijklmnopqrstuvwxyz\n' >"$INSTALL_DIR/.env"
prepare_environment </dev/null >/dev/null
grep -qx 'GROUP_ACCESS_MODE=open' "$INSTALL_DIR/.env"
