#!/usr/bin/env bash
set -euo pipefail

# LinkDownloaderBotForGroups installer (Ubuntu 22/24)
# - asks only for BOT_TOKEN
# - Docker (recommended) or systemd mode
# - safe: makes backups before changing compose, avoids conflicts, minimal side effects

REPO_URL_DEFAULT="https://github.com/Avazbek22/LinkDownloaderBotForGroups.git"
BRANCH_DEFAULT="main"
INSTALL_DIR_DEFAULT="$HOME/LinkDownloaderBotForGroups"

SERVICE_NAME_DEFAULT="linkdownloaderbot"
CONTAINER_NAME_DEFAULT="linkdownloaderbot"

MAX_FILE_MB=50
MAX_FILE_BYTES=$((MAX_FILE_MB * 1024 * 1024))

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l

say()  { echo -e "\n\033[1m\033[36m$*\033[0m"; }
ok()   { echo -e "\033[32m✔\033[0m $*"; }
warn() { echo -e "\033[33m⚠\033[0m $*"; }
err()  { echo -e "\033[31m✖\033[0m $*" >&2; }

need_cmd() { command -v "$1" >/dev/null 2>&1; }

as_root() {
  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

prompt_yn() {
  local prompt="$1"
  local def="${2:-N}"
  local suffix="[y/N]"
  [[ "$def" == "Y" ]] && suffix="[Y/n]"
  read -r -p "$prompt $suffix: " ans || true
  ans="${ans:-$def}"
  case "${ans,,}" in
    y|yes) return 0 ;;
    n|no)  return 1 ;;
    *) [[ "$def" == "Y" ]] && return 0 || return 1 ;;
  esac
}

is_ubuntu() {
  [[ -f /etc/os-release ]] && grep -qi "ubuntu" /etc/os-release
}

has_systemd() {
  need_cmd systemctl && [[ "$(ps -p 1 -o comm= 2>/dev/null || true)" == "systemd" ]]
}

detect_compose() {
  # prints: "docker compose" or "docker-compose" or empty
  if need_cmd docker; then
    if docker compose version >/dev/null 2>&1; then
      echo "docker compose"
      return 0
    fi
    if need_cmd docker-compose; then
      echo "docker-compose"
      return 0
    fi
  fi
  echo ""
  return 1
}

ensure_universe_enabled() {
  if ! grep -Rqs "^[^#].*ubuntu.* universe" /etc/apt/sources.list /etc/apt/sources.list.d/*.list 2>/dev/null; then
    warn "Ubuntu 'universe' repository seems disabled."
    if prompt_yn "Enable 'universe' repository?" "Y"; then
      as_root apt-get update -y
      as_root apt-get install -y --no-install-recommends software-properties-common
      as_root add-apt-repository -y universe
      as_root apt-get update -y
      ok "Universe enabled."
    else
      warn "Universe not enabled. Some packages may be unavailable."
    fi
  fi
}

install_base_deps() {
  say "Installing base dependencies (git, curl, python3, venv, pip, ffmpeg)..."
  as_root apt-get update -y
  as_root apt-get install -y --no-install-recommends \
    ca-certificates curl git python3 python3-venv python3-pip ffmpeg
  ok "Base dependencies installed."
}

clone_or_update_repo() {
  local repo_url="$1"
  local branch="$2"
  local install_dir="$3"

  if [[ -d "$install_dir/.git" ]]; then
    say "Repository exists: $install_dir"
    say "Updating (git pull)..."
    git -C "$install_dir" fetch --all --prune
    git -C "$install_dir" checkout "$branch" >/dev/null 2>&1 || true
    git -C "$install_dir" pull --ff-only || true
    ok "Repository updated."
  else
    if [[ -e "$install_dir" && ! -d "$install_dir" ]]; then
      err "Install path exists but is not a directory: $install_dir"
      exit 1
    fi
    if [[ -d "$install_dir" && -n "$(ls -A "$install_dir" 2>/dev/null || true)" ]]; then
      warn "Install directory is not empty: $install_dir"
      warn "To avoid overwriting чужих файлов, укажете другой INSTALL_DIR или очистите папку."
      exit 1
    fi
    say "Cloning repository into: $install_dir"
    git clone --branch "$branch" --single-branch "$repo_url" "$install_dir"
    ok "Repository cloned."
  fi
}

read_bot_token() {
  local token=""
  while [[ -z "$token" ]]; do
    read -r -s -p "Enter Telegram Bot Token: " token || true
    echo
    token="$(echo -n "$token" | tr -d '\r\n' | xargs)"
    [[ -z "$token" ]] && warn "Token can't be empty."
  done

  # soft validation (do not block)
  if ! echo "$token" | grep -Eq '^[0-9]{6,}:[A-Za-z0-9_-]{20,}$'; then
    warn "Token looks unusual. I'll continue anyway."
  fi

  echo "$token"
}

write_env_and_config() {
  local install_dir="$1"
  local token="$2"

  # Ensure data dir exists (for prefs.json)
  mkdir -p "$install_dir/data"

  # .env (local secret)
  cat > "$install_dir/.env" <<EOF
BOT_TOKEN=$token
OUTPUT_FOLDER=/tmp/yt-dlp-telegram
EOF

  # config.py (local secret fallback) — works in both docker/systemd even if env not passed
  cat > "$install_dir/config.py" <<PY
import os

# Prefer env; fallback to local token (kept outside git)
token = (os.getenv("BOT_TOKEN") or "${token}").strip()
if not token:
    raise RuntimeError("BOT_TOKEN is not set. Put it into .env or environment variables.")

# Optional logs chat id (disabled by default)
logs = None

# Fixed limit (${MAX_FILE_MB} MB)
max_filesize = ${MAX_FILE_BYTES}

# Temp folder for downloads
output_folder = (os.getenv("OUTPUT_FOLDER") or "/tmp/yt-dlp-telegram").strip() or "/tmp/yt-dlp-telegram"

# Optional: cookies file inside container/host (disabled by default)
cookies_file = None
PY

  ok "Created .env + config.py (not tracked by git)."
}

ensure_docker_installed() {
  if need_cmd docker; then
    ok "Docker found."
    return 0
  fi

  warn "Docker is not installed."
  if prompt_yn "Install Docker (docker.io) from Ubuntu repositories?" "Y"; then
    ensure_universe_enabled
    as_root apt-get update -y
    as_root apt-get install -y --no-install-recommends docker.io
    as_root systemctl enable --now docker >/dev/null 2>&1 || true
    ok "Docker installed."
    return 0
  fi

  return 1
}

ensure_compose_available() {
  local compose_cmd
  compose_cmd="$(detect_compose || true)"
  if [[ -n "$compose_cmd" ]]; then
    ok "Compose found: $compose_cmd"
    return 0
  fi

  warn "Docker Compose not detected."
  ensure_universe_enabled

  # Try plugin first
  if prompt_yn "Install docker compose plugin (recommended)?" "Y"; then
    as_root apt-get install -y --no-install-recommends docker-compose-plugin >/dev/null 2>&1 || true
  fi

  compose_cmd="$(detect_compose || true)"
  if [[ -n "$compose_cmd" ]]; then
    ok "Compose is ready: $compose_cmd"
    return 0
  fi

  # Fallback v1
  warn "Falling back to docker-compose (v1) package..."
  as_root apt-get install -y --no-install-recommends docker-compose >/dev/null 2>&1 || true

  compose_cmd="$(detect_compose || true)"
  if [[ -n "$compose_cmd" ]]; then
    ok "Compose is ready: $compose_cmd"
    return 0
  fi

  err "Failed to install or detect Docker Compose."
  return 1
}

ensure_docker_permissions() {
  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    return 0
  fi

  if docker ps >/dev/null 2>&1; then
    return 0
  fi

  if docker ps 2>&1 | grep -qi "permission denied"; then
    warn "Docker socket permission denied for user '$USER'."
    if prompt_yn "Add user '$USER' to docker group (recommended)?" "Y"; then
      as_root usermod -aG docker "$USER" || true
      ok "User added to docker group."
      warn "Важно: выйдите и зайдите в SSH заново (или перезайдите в сессию), чтобы права применились."
    fi
  fi
}

detect_service_name_from_compose() {
  local compose_file="$1"
  if [[ ! -f "$compose_file" ]]; then
    echo "$SERVICE_NAME_DEFAULT"
    return 0
  fi

  # First service under services:
  local s
  s="$(awk '
    BEGIN{in=0}
    /^[[:space:]]*services:[[:space:]]*$/ {in=1; next}
    in==1 && /^[[:space:]]{2}[A-Za-z0-9_-]+:[[:space:]]*$/ {
      gsub(":","",$1); print $1; exit
    }
  ' "$compose_file" 2>/dev/null || true)"

  echo "${s:-$SERVICE_NAME_DEFAULT}"
}

backup_file() {
  local path="$1"
  if [[ -f "$path" ]]; then
    local ts
    ts="$(date +%Y%m%d-%H%M%S)"
    cp -f "$path" "${path}.bak.${ts}"
    ok "Backup created: ${path}.bak.${ts}"
  fi
}

patch_docker_compose_safely() {
  local install_dir="$1"
  local compose_path="$install_dir/docker-compose.yml"
  local service_name="$2"

  if [[ ! -f "$compose_path" ]]; then
    warn "docker-compose.yml not found in repo. I'll create a minimal one."
    cat > "$compose_path" <<YML
services:
  ${service_name}:
    build: .
    container_name: ${CONTAINER_NAME_DEFAULT}
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ./config.py:/app/config.py:ro
      - ./data:/app/data
YML
    ok "docker-compose.yml created."
    return 0
  fi

  backup_file "$compose_path"

  # Patch with python (more reliable than sed)
  python3 - <<PY
import re
from pathlib import Path

compose_path = Path(r"$compose_path")
service_name = r"$service_name"

text = compose_path.read_text(encoding="utf-8", errors="replace").splitlines(True)

# Very small, safe patcher for typical compose structure.
# Ensures in target service:
#   env_file: [.env]
#   volumes includes ./config.py and ./data
#
# If something is unusual, we keep file unchanged.

def find_service_block(lines, svc):
    # returns (start_idx, end_idx) for block lines belonging to service under services:
    # expected indentation: "  svc:"
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^\\s{{2}}{re.escape(svc)}:\\s*$", line):
            start = i
            break
    if start is None:
        return None

    # end is next service with same indent or EOF
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^\\s{2}[A-Za-z0-9_-]+:\\s*$", lines[j]):
            end = j
            break
    return start, end

block = find_service_block(text, service_name)
if block is None:
    # If service name not found, do not rewrite unpredictably.
    print("NO_SERVICE")
    raise SystemExit(0)

start, end = block
block_lines = text[start:end]

block_str = "".join(block_lines)

def has_env_file(s):
    return re.search(r"^\\s{4}env_file:\\s*$", s, flags=re.M) is not None

def has_volume_line(s, vol):
    # volumes list item indentation is usually 6 spaces
    return re.search(rf"^\\s{{6}}-\\s*{re.escape(vol)}\\s*$", s, flags=re.M) is not None

need_env = not has_env_file(block_str)

need_config_vol = not has_volume_line(block_str, "./config.py:/app/config.py:ro")
need_data_vol   = not has_volume_line(block_str, "./data:/app/data")

# Ensure volumes key exists if any volume is missing
has_volumes_key = re.search(r"^\\s{4}volumes:\\s*$", block_str, flags=re.M) is not None

new_block = block_lines[:]

def insert_before_volumes(lines, insertion_lines):
    # place insertion just before "    volumes:" if exists else before end
    out = []
    inserted = False
    for ln in lines:
        if (not inserted) and re.match(r"^\\s{4}volumes:\\s*$", ln):
            out.extend(insertion_lines)
            inserted = True
        out.append(ln)
    if not inserted:
        # insert before end of service block
        # keep last newline style
        if out and not out[-1].endswith("\\n"):
            out[-1] = out[-1] + "\\n"
        out.extend(insertion_lines)
    return out

# Add env_file if missing (insert before volumes or end)
if need_env:
    env_lines = [
        "    env_file:\\n",
        "      - .env\\n",
    ]
    new_block = insert_before_volumes(new_block, env_lines)

# Ensure volumes section and items
block_str2 = "".join(new_block)
if (need_config_vol or need_data_vol) and (not re.search(r"^\\s{4}volumes:\\s*$", block_str2, flags=re.M)):
    # Add volumes section at end
    if not new_block[-1].endswith("\\n"):
        new_block[-1] = new_block[-1] + "\\n"
    new_block.extend([
        "    volumes:\\n",
    ])

# Now append missing volume items right after volumes:
out = []
in_volumes = False
volumes_indent = None
for ln in new_block:
    out.append(ln)
    if re.match(r"^\\s{4}volumes:\\s*$", ln):
        in_volumes = True
        volumes_indent = "      "
        # immediately inject missing items right after volumes header
        if need_config_vol:
            out.append(f"{volumes_indent}- ./config.py:/app/config.py:ro\\n")
        if need_data_vol:
            out.append(f"{volumes_indent}- ./data:/app/data\\n")
        in_volumes = False  # injected, done (do not try to be clever further)

new_block = out

# Write back only if changed
new_text = text[:start] + new_block + text[end:]
if new_text != text:
    compose_path.write_text("".join(new_text), encoding="utf-8")
    print("PATCHED")
else:
    print("NO_CHANGE")
PY

  local res="$?"
  if [[ "$res" -ne 0 ]]; then
    warn "Compose patcher returned non-zero. I kept your docker-compose.yml as-is."
    return 0
  fi

  ok "docker-compose.yml patched safely (env_file + data volume ensured)."
}

stop_conflicting_systemd_if_any() {
  local service="$1"
  if ! has_systemd; then
    return 0
  fi
  if systemctl list-units --type=service --all 2>/dev/null | grep -qE "\\b${service}\\.service\\b"; then
    if systemctl is-active --quiet "$service" 2>/dev/null; then
      warn "Systemd service '$service' is running. Это может дать 409 конфликт (две копии бота)."
      if prompt_yn "Stop systemd service '$service' now?" "Y"; then
        as_root systemctl stop "$service" || true
        ok "Systemd service stopped."
      else
        warn "I will continue, but conflict may happen."
      fi
    fi
  fi
}

stop_conflicting_container_if_any() {
  local container="$1"
  if need_cmd docker && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$container"; then
    warn "Docker container '$container' is running. Это может дать 409 конфликт."
    if prompt_yn "Stop container '$container' now?" "Y"; then
      docker stop "$container" >/dev/null 2>&1 || true
      ok "Container stopped."
    else
      warn "I will continue, but conflict may happen."
    fi
  fi
}

run_with_compose() {
  local install_dir="$1"

  local compose_cmd
  compose_cmd="$(detect_compose)"
  say "Starting with Docker Compose..."
  (cd "$install_dir" && $compose_cmd up -d --build)

  ok "Bot started in Docker."
  echo
  echo "Logs:"
  echo "  cd \"$install_dir\" && ($compose_cmd logs -f --tail=200)"
  echo "Stop:"
  echo "  cd \"$install_dir\" && ($compose_cmd down)"
}

install_system_mode() {
  local install_dir="$1"
  local service="$2"

  say "Installing in system mode (venv + systemd if available)..."

  if [[ ! -f "$install_dir/requirements.txt" || ! -f "$install_dir/main.py" ]]; then
    err "Repo files not found (requirements.txt/main.py). Wrong install dir?"
    exit 1
  fi

  python3 -m venv "$install_dir/.venv"
  "$install_dir/.venv/bin/pip" install --upgrade pip >/dev/null 2>&1 || true
  "$install_dir/.venv/bin/pip" install -r "$install_dir/requirements.txt"
  ok "Python venv ready."

  if has_systemd; then
    local unit="/etc/systemd/system/${service}.service"

    if [[ -f "$unit" ]]; then
      warn "Systemd unit already exists: $unit"
      warn "Я не буду перезаписывать его автоматически."
      echo
      echo "If you want to replace it manually:"
      echo "  sudo rm -f \"$unit\""
      echo "  then re-run install.sh and choose systemd mode."
      return 0
    fi

    say "Creating systemd service: ${service}"

    as_root tee "$unit" >/dev/null <<EOF
[Unit]
Description=LinkDownloaderBotForGroups (Telegram)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$install_dir
EnvironmentFile=$install_dir/.env
ExecStart=$install_dir/.venv/bin/python -u $install_dir/main.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

    as_root systemctl daemon-reload
    as_root systemctl enable --now "$service"
    ok "Service started."

    echo
    echo "Status:"
    echo "  sudo systemctl status $service"
    echo "Logs:"
    echo "  sudo journalctl -u $service -f --no-pager"
    echo "Stop:"
    echo "  sudo systemctl stop $service"
  else
    warn "systemd is not available (PID1 is not systemd)."
    echo
    echo "Run manually:"
    echo "  cd \"$install_dir\""
    echo "  set -a; source .env; set +a"
    echo "  \"$install_dir/.venv/bin/python\" -u main.py"
  fi
}

main() {
  local repo_url="${REPO_URL:-$REPO_URL_DEFAULT}"
  local branch="${BRANCH:-$BRANCH_DEFAULT}"
  local install_dir="${INSTALL_DIR:-$INSTALL_DIR_DEFAULT}"

  local service_name="${SERVICE_NAME:-$SERVICE_NAME_DEFAULT}"
  local container_name="${CONTAINER_NAME:-$CONTAINER_NAME_DEFAULT}"

  echo
  say "LinkDownloaderBotForGroups installer (Ubuntu 22/24)"
  echo "Repo:   $repo_url"
  echo "Branch: $branch"
  echo "Dir:    $install_dir"
  echo

  if ! is_ubuntu; then
    warn "This installer is designed for Ubuntu 22.04/24.04. Continuing anyway..."
  fi

  install_base_deps
  clone_or_update_repo "$repo_url" "$branch" "$install_dir"

  local token
  token="$(read_bot_token)"
  write_env_and_config "$install_dir" "$token"

  # Decide mode
  local want_docker="Y"
  if ! need_cmd docker && has_systemd; then
    want_docker="N"
  fi

  if prompt_yn "Install & run using Docker (recommended)?" "$want_docker"; then
    stop_conflicting_systemd_if_any "$service_name"

    ensure_docker_installed || { err "Docker required for Docker mode."; exit 1; }
    ensure_docker_permissions || true
    ensure_compose_available || { err "Compose required for Docker mode."; exit 1; }

    local compose_file="$install_dir/docker-compose.yml"
    local svc
    svc="$(detect_service_name_from_compose "$compose_file")"

    patch_docker_compose_safely "$install_dir" "$svc"

    stop_conflicting_container_if_any "$container_name"
    run_with_compose "$install_dir"
  else
    stop_conflicting_container_if_any "$container_name"
    install_system_mode "$install_dir" "$service_name"
  fi

  echo
  ok "Done."
}

main "$@"
