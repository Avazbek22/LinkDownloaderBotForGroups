#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/Avazbek22/LinkDownloaderBotForGroups"
BRANCH="main"

install_dir="${INSTALL_DIR:-$PWD/LinkDownloaderBotForGroups}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-linkdownloaderbotforgroups}"
SERVICE_KEY="${SERVICE_KEY:-linkdownloaderbot}"   # имя сервиса в docker-compose.yml
UPDATER_SERVICE_NAME="${UPDATER_SERVICE_NAME:-linkdownloaderbotforgroups-yt-dlp-update.service}"
UPDATER_TIMER_NAME="${UPDATER_TIMER_NAME:-linkdownloaderbotforgroups-yt-dlp-update.timer}"

PYTHON_BIN="python3"
VENV_DIR=".venv"

# ---------- UI helpers ----------
say()  { echo -e "\n\033[1m\033[36m$*\033[0m"; }
ok()   { echo -e "\033[32m✔\033[0m $*"; }
warn() { echo -e "\033[33m⚠\033[0m $*" >&2; }
die()  { echo -e "\033[31m✖\033[0m $*" >&2; exit 1; }

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
  local ans=""
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

# ---------- deps ----------
ensure_deps_basic() {
  say "Installing dependencies"
  apt_install git curl ca-certificates ffmpeg nodejs "$PYTHON_BIN" "$PYTHON_BIN-venv" "$PYTHON_BIN-pip" || true
  ok "Dependencies are installed (or already present)."
}

ensure_env_key() {
  local env_file="$1"
  local key="$2"
  local value="$3"

  if [[ ! -f "$env_file" ]]; then
    return 0
  fi

  if grep -q "^${key}=" "$env_file"; then
    return 0
  fi

  echo "${key}=${value}" >> "$env_file"
}

# ---------- repo ----------
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

# ---------- token / env ----------
mask_token() {
  local t="$1"
  local len="${#t}"
  if (( len <= 12 )); then
    echo "$len chars"
    return 0
  fi
  echo "${t:0:5}...${t: -5} ($len chars)"
}

read_bot_token() {
  local token="${BOT_TOKEN:-}"

  if [[ -z "$token" ]]; then
    # ВАЖНО: всё, что не токен — только в stderr, чтобы $(read_bot_token) не захватывал мусор
    warn ""
    warn "Enter your Telegram bot token."
    warn "Input is hidden by default (security). Paste the token and press Enter."
    warn "If you really want visible input, run: SHOW_TOKEN_INPUT=1 ./install.sh"

    if [[ "${SHOW_TOKEN_INPUT:-0}" == "1" ]]; then
      >&2 printf "BOT_TOKEN: "
      read -r token || true
    else
      >&2 printf "BOT_TOKEN: "
      read -r -s token || true
      >&2 echo
    fi
  fi

  # cleanup (handles Windows CRLF and accidental spaces)
  token="$(printf '%s' "$token" | tr -d '\r\n' | xargs)"

  if [[ -z "$token" ]]; then
    die "Empty BOT_TOKEN."
  fi

  # basic sanity check (warn only)
  if [[ ! "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then
    warn "BOT_TOKEN format looks unusual. If the bot doesn't start, re-check the token in BotFather."
  fi

  # stdout MUST contain only token
  printf '%s' "$token"
}

write_env_and_config() {
  local env_path="$install_dir/.env"
  local cfg_path="$install_dir/config.py"

  say "Config"

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
    {
      echo "BOT_TOKEN=$token"
      echo "# Optional:"
      echo "# LOGS_CHAT_ID=123456789"
      echo "# MAX_FILESIZE=52428800"
      echo "# OUTPUT_FOLDER=/tmp/yt-dlp-telegram"
      echo "# COOKIES_FILE=/app/cookies.txt"
      echo "YTDLP_JS_RUNTIMES=node"
      echo "YTDLP_REMOTE_COMPONENTS=ejs:github"
      echo "YTDLP_INSTAGRAM_IMPERSONATE=chrome"
      echo "YTDLP_INSTAGRAM_RETRIES=8"
      echo "YTDLP_INSTAGRAM_FRAGMENT_RETRIES=8"
      echo "YTDLP_INSTAGRAM_SOCKET_TIMEOUT=30"
    } > "$env_path"
    chmod 600 "$env_path" 2>/dev/null || true
    ok ".env written: $(mask_token "$token")"
  fi

  ensure_env_key "$env_path" "YTDLP_JS_RUNTIMES" "node"
  ensure_env_key "$env_path" "YTDLP_REMOTE_COMPONENTS" "ejs:github"
  ensure_env_key "$env_path" "YTDLP_INSTAGRAM_IMPERSONATE" "chrome"
  ensure_env_key "$env_path" "YTDLP_INSTAGRAM_RETRIES" "8"
  ensure_env_key "$env_path" "YTDLP_INSTAGRAM_FRAGMENT_RETRIES" "8"
  ensure_env_key "$env_path" "YTDLP_INSTAGRAM_SOCKET_TIMEOUT" "30"

  # config.py без секретов (как у Вас сейчас рабочий вариант)
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

# ---------- docker ----------
ensure_docker() {
  say "Docker"
  if ! need_cmd docker; then
    warn "Docker not found. Installing docker.io..."
    apt_install docker.io
  fi

  local compose_cmd=""
  compose_cmd="$(detect_compose)" || true
  if [[ -z "${compose_cmd:-}" ]]; then
    warn "Docker Compose not found. Installing docker-compose..."
    apt_install docker-compose
  fi

  ok "Docker is ready."
}

ensure_compose_override() {
  # НЕ ТРОГАЕМ docker-compose.yml (чтобы git pull не конфликтовал).
  # Делаем docker-compose.override.yml, который compose подхватит автоматически.
  local override_path="$install_dir/docker-compose.override.yml"

  say "Docker Compose override"
  cat > "$override_path" <<YML
services:
  ${SERVICE_KEY}:
    env_file:
      - .env
    volumes:
      - ./config.py:/app/config.py:ro
      - ./data:/app/data
YML

  ok "Written: docker-compose.override.yml (no changes to docker-compose.yml)."
}

stop_conflicting_containers() {
  say "Stopping old container (if any)"
  docker rm -f linkdownloaderbot 2>/dev/null || true
  ok "Done."
}

run_with_compose() {
  say "Running with Docker Compose"
  local compose_cmd
  compose_cmd="$(detect_compose)" || die "docker compose / docker-compose not found"

  cd "$install_dir"

  # data folder for prefs.json etc.
  mkdir -p data

  $compose_cmd -p "$COMPOSE_PROJECT" up -d --build
  ok "Started. Logs:"
  echo "  cd \"$install_dir\""
  echo "  $compose_cmd -p \"$COMPOSE_PROJECT\" logs -f --tail=200"
}

# ---------- system mode ----------
install_system_mode() {
  say "System mode (venv)"
  cd "$install_dir"

  if [[ ! -d "$VENV_DIR" ]]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    ok "venv created: $VENV_DIR"
  fi

  "$VENV_DIR/bin/pip" install -r requirements.txt
  ok "Dependencies installed."

  warn "Systemd generation is not included here; Docker mode is recommended on servers."
  echo "Run manually:"
  echo "  cd \"$install_dir\""
  echo "  $VENV_DIR/bin/python main.py"
}

install_nightly_updater() {
  say "Nightly yt-dlp updater"

  if ! need_cmd systemctl || [[ ! -d /run/systemd/system ]]; then
    warn "systemd is unavailable. Skipping nightly updater setup (graceful fallback)."
    return 0
  fi

  local service_tpl="$install_dir/scripts/systemd/linkdownloaderbotforgroups-yt-dlp-update.service"
  local timer_tpl="$install_dir/scripts/systemd/linkdownloaderbotforgroups-yt-dlp-update.timer"
  local service_out="/etc/systemd/system/$UPDATER_SERVICE_NAME"
  local timer_out="/etc/systemd/system/$UPDATER_TIMER_NAME"

  [[ -f "$service_tpl" ]] || { warn "Missing $service_tpl"; return 0; }
  [[ -f "$timer_tpl" ]] || { warn "Missing $timer_tpl"; return 0; }

  sed \
    -e "s|__INSTALL_DIR__|$install_dir|g" \
    -e "s|__COMPOSE_PROJECT__|$COMPOSE_PROJECT|g" \
    -e "s|__SERVICE_KEY__|$SERVICE_KEY|g" \
    "$service_tpl" | as_root tee "$service_out" >/dev/null

  as_root cp "$timer_tpl" "$timer_out"

  as_root systemctl daemon-reload
  as_root systemctl enable --now "$UPDATER_TIMER_NAME"
  ok "Nightly updater enabled: $UPDATER_TIMER_NAME"
}

main() {
  say "LinkDownloaderBotForGroups installer"

  ensure_deps_basic
  clone_or_update_repo
  write_env_and_config

  if read_yes_no "Install & run using Docker? [Y/n]" "Y"; then
    ensure_docker
    ensure_compose_override
    stop_conflicting_containers
    run_with_compose
  else
    install_system_mode
  fi

  install_nightly_updater
}

main "$@"
