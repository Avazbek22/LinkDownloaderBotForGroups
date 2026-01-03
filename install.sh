#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/Avazbek22/LinkDownloaderBotForGroups"
BRANCH="main"
install_dir="${INSTALL_DIR:-$PWD/LinkDownloaderBotForGroups}"
SERVICE_NAME="linkdownloaderbot"
COMPOSE_PROJECT="linkdownloaderbotforgroups"
PYTHON_BIN="python3"
VENV_DIR=".venv"

say() { echo -e "\n\033[1m\033[36m$*\033[0m"; }
ok()  { echo -e "\033[32m✔\033[0m $*"; }
warn(){ echo -e "\033[33m⚠\033[0m $*" >&2; }
die() { echo -e "\033[31m✖\033[0m $*" >&2; exit 1; }

need_cmd() { command -v "$1" >/dev/null 2>&1; }

as_root() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    "$@"
  else
    need_cmd sudo || die "sudo not found. Run as root or install sudo."
    sudo "$@"
  fi
}

apt_install() {
  as_root apt-get update -y
  as_root apt-get install -y --no-install-recommends "$@"
}

read_yes_no() {
  local prompt="$1"
  local default="${2:-Y}"
  local ans
  read -r -p "$prompt " ans || true
  ans="${ans:-$default}"
  case "$ans" in
    Y|y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

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

ensure_deps_basic() {
  say "Installing dependencies"
  apt_install git curl ca-certificates ffmpeg "$PYTHON_BIN" "$PYTHON_BIN-venv" "$PYTHON_BIN-pip
" || true
  ok "Dependencies are installed (or already present)."
}

clone_or_update_repo() {
  say "Repository"
  if [[ -d "$install_dir/.git" ]]; then
    ok "Repo already exists: $install_dir"
    git -C "$install_dir" fetch --all --prune
    git -C "$install_dir" checkout "$BRANCH"
    git -C "$install_dir" pull --ff-only
  else
    git clone --branch "$BRANCH" "$REPO" "$install_dir"
  fi
  ok "Repo is ready."
}

read_bot_token() {
  local token="${BOT_TOKEN:-}"

  if [[ -z "$token" ]]; then
    say "Enter your Telegram bot token."
    warn "Input is hidden by default (security). Paste the token and press Enter."
    warn "If you really want the token to be visible while typing/pasting, run: SHOW_TOKEN_INPUT=1 ./install.sh"
    if [[ "${SHOW_TOKEN_INPUT:-0}" == "1" ]]; then
      read -r -p "BOT_TOKEN: " token
    else
      read -r -s -p "BOT_TOKEN: " token
      echo
    fi
  fi

  # cleanup (handles Windows CRLF and accidental spaces)
  token="$(printf '%s' "$token" | tr -d '\r\n' | xargs)"

  if [[ -z "$token" ]]; then
    die "Empty BOT_TOKEN."
  fi

  # basic sanity check (do not block on it, just warn)
  if [[ ! "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then
    warn "BOT_TOKEN format looks unusual. If the bot doesn't start, re-check the token in BotFather."
  fi

  echo "$token"
}

mask_token() {
  local t="$1"
  local len="${#t}"
  if (( len <= 12 )); then
    echo "$len chars"
    return 0
  fi
  echo "${t:0:5}...${t: -5} ($len chars)"
}

write_env_and_config() {
  local env_path="$install_dir/.env"
  local cfg_path="$install_dir/config.py"

  say "Config"

  # 1) Ensure .env with BOT_TOKEN (do not overwrite a non-empty existing token unless forced)
  local existing_token=""
  if [[ -f "$env_path" ]]; then
    existing_token="$(grep -m1 '^BOT_TOKEN=' "$env_path" | cut -d= -f2- | tr -d '\r\n' | xargs || true)"
  fi

  local token=""
  if [[ -n "${BOT_TOKEN:-}" ]]; then
    token="$(printf '%s' "$BOT_TOKEN" | tr -d '\r\n' | xargs)"
    ok "Using BOT_TOKEN from current environment: $(mask_token "$token")"
  elif [[ -n "$existing_token" && "${FORCE_ENV:-0}" != "1" ]]; then
    token="$existing_token"
    ok "Using existing .env BOT_TOKEN: $(mask_token "$token")"
  else
    token="$(read_bot_token)"
    printf 'BOT_TOKEN=%s\n' "$token" > "$env_path"
    chmod 600 "$env_path" 2>/dev/null || true
    ok ".env written: $(mask_token "$token")"
  fi

  # 2) Create config.py (no secrets inside). Keep existing unless forced.
  if [[ -f "$cfg_path" && "${FORCE_CONFIG:-0}" != "1" ]]; then
    ok "config.py already exists — keeping it."
    return 0
  fi

  cat > "$cfg_path" <<'PY'
import os

# Required: Telegram bot token (from .env or environment variables)
token = (os.getenv("BOT_TOKEN") or "").strip()
if not token:
    raise RuntimeError("BOT_TOKEN is not set. Put it into .env or environment variables.")

# Optional logs chat id (disabled by default)
logs_raw = (os.getenv("LOGS_CHAT_ID") or "").strip()
if logs_raw:
    try:
        logs = int(logs_raw)
    except ValueError as ex:
        raise RuntimeError("LOGS_CHAT_ID must be an integer.") from ex
else:
    logs = None

# Max file size (bytes). Default: 50 MB.
max_filesize_raw = (os.getenv("MAX_FILESIZE") or "").strip()
if max_filesize_raw:
    try:
        max_filesize = int(max_filesize_raw)
    except ValueError as ex:
        raise RuntimeError("MAX_FILESIZE must be an integer (bytes).") from ex
else:
    max_filesize = 50 * 1024 * 1024

# Temp folder for downloads (can be overridden)
output_folder = (os.getenv("OUTPUT_FOLDER") or "/tmp/yt-dlp-telegram").strip() or "/tmp/yt-dlp-telegram"

# Optional: cookies file path (disabled by default)
cookies_file = (os.getenv("COOKIES_FILE") or "").strip() or None
PY

  ok "config.py written (no secrets)."
}

patch_docker_compose_safely() {
  local compose_path="$install_dir/docker-compose.yml"
  [[ -f "$compose_path" ]] || die "docker-compose.yml not found in $install_dir"

  say "docker-compose.yml patch"

  cp -a "$compose_path" "$compose_path.bak.$(date +%s)"

  # Ensure env_file: .env
  if ! grep -qE '^\s*env_file:\s*$' "$compose_path"; then
    awk '
      BEGIN{added=0}
      /container_name:/ && added==0{
        print $0
        print "    env_file:"
        print "      - .env"
        added=1
        next
      }
      {print $0}
      END{
        if(added==0){
          # fallback: add under service block start
        }
      }
    ' "$compose_path" > "$compose_path.tmp" && mv "$compose_path.tmp" "$compose_path"
    ok "Added env_file: .env"
  else
    ok "env_file already present"
  fi

  # Ensure volumes include config and data (main.py is already present in repo compose)
  if ! grep -q './config.py:/app/config.py:ro' "$compose_path"; then
    awk '
      BEGIN{invol=0; added=0}
      /^\s*volumes:\s*$/ {invol=1; print; next}
      invol==1 && added==0 && /^\s*-\s*\.\// {
        print "      - ./config.py:/app/config.py:ro"
        added=1
      }
      {print}
    ' "$compose_path" > "$compose_path.tmp" && mv "$compose_path.tmp" "$compose_path"
    ok "Ensured config.py mount"
  else
    ok "config.py mount already present"
  fi

  if ! grep -q './data:/app/data' "$compose_path"; then
    awk '
      BEGIN{invol=0}
      /^\s*volumes:\s*$/ {invol=1; print; next}
      invol==1 && /^\s*-\s*\./ {print; next}
      invol==1 && /^\s*$/ {print "      - ./data:/app/data"; invol=0; next}
      {print}
    ' "$compose_path" > "$compose_path.tmp" && mv "$compose_path.tmp" "$compose_path"
    ok "Ensured data mount"
  else
    ok "data mount already present"
  fi
}

stop_conflicting_containers() {
  say "Stopping conflicting containers (if any)"
  if need_cmd docker; then
    docker ps -a --format '{{.Names}}' | grep -q "^$SERVICE_NAME$" && docker rm -f "$SERVICE_NAME" >/dev/null 2>&1 || true
  fi
  ok "Done."
}

ensure_docker() {
  say "Docker"
  if ! need_cmd docker; then
    warn "Docker not found. Installing docker.io..."
    apt_install docker.io
  fi

  local compose_cmd
  compose_cmd="$(detect_compose)" || true
  if [[ -z "${compose_cmd:-}" ]]; then
    warn "Docker Compose not found. Installing docker-compose..."
    apt_install docker-compose
  fi
  ok "Docker is ready."
}

run_with_compose() {
  say "Running with Docker Compose"
  local compose_cmd
  compose_cmd="$(detect_compose)" || die "docker compose / docker-compose not found"
  cd "$install_dir"
  $compose_cmd -p "$COMPOSE_PROJECT" up -d --build
  ok "Started. Check logs:"
  echo "  cd \"$install_dir\""
  echo "  $compose_cmd -p \"$COMPOSE_PROJECT\" logs -f --tail=200"
}

install_system_mode() {
  say "System mode (venv)"
  cd "$install_dir"
  if [[ ! -d "$VENV_DIR" ]]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    ok "venv created: $VENV_DIR"
  fi
  "$VENV_DIR/bin/pip" install -r requirements.txt
  ok "Dependencies installed."

  warn "Systemd service generation is not included here; Docker mode is recommended on servers."
  echo "Run manually:"
  echo "  cd \"$install_dir\""
  echo "  $VENV_DIR/bin/python main.py"
}

main() {
  say "LinkDownloaderBotForGroups installer"

  ensure_deps_basic
  clone_or_update_repo
  write_env_and_config

  if read_yes_no "Install & run using Docker? [Y/n]" "Y"; then
    ensure_docker
    patch_docker_compose_safely
    stop_conflicting_containers
    run_with_compose
  else
    install_system_mode
  fi
}

main "$@"
