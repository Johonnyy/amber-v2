#!/usr/bin/env bash
#
# Amber first-time setup for the OVH VPS (systemd).
# Interactive: prompts for the bits that can't be guessed (API key, etc.).
# Idempotent: safe to re-run — it skips work that's already done.
#
# Usage (as root):
#   curl -fsSL https://raw.githubusercontent.com/Johonnyy/amber-v2/main/deploy/setup.sh | sudo bash
#   # or, if you've already cloned the repo:
#   sudo bash deploy/setup.sh
#
set -euo pipefail

REPO_URL="https://github.com/Johonnyy/amber-v2"
APP_DIR="/opt/amber"
APP_USER="amber"
ENV_FILE="$APP_DIR/.env"
SERVICE_SRC="$APP_DIR/deploy/amber.service"
SERVICE_DST="/etc/systemd/system/amber.service"
PYTHON=""   # resolved in step 1; the app requires Python >= 3.11

# --- pretty output -----------------------------------------------------------
c_blue=$'\033[1;34m'; c_green=$'\033[1;32m'; c_yellow=$'\033[1;33m'; c_red=$'\033[1;31m'; c_off=$'\033[0m'
step()  { echo; echo "${c_blue}==>${c_off} $*"; }
ok()    { echo "${c_green} ok${c_off} $*"; }
warn()  { echo "${c_yellow} ! ${c_off} $*"; }
die()   { echo "${c_red}error:${c_off} $*" >&2; exit 1; }

ask() {  # ask "Prompt" "default"  -> echoes answer
  local prompt="$1" default="${2:-}" reply
  if [ -n "$default" ]; then
    read -rp "$prompt [$default]: " reply </dev/tty
    echo "${reply:-$default}"
  else
    read -rp "$prompt: " reply </dev/tty
    echo "$reply"
  fi
}

ask_secret() {  # ask_secret "Prompt"  -> echoes answer, input hidden
  local prompt="$1" reply
  read -rsp "$prompt: " reply </dev/tty; echo >&2
  echo "$reply"
}

[ "$(id -u)" -eq 0 ] || die "run as root (try: sudo bash $0)"

# Is $1 a python that's >= 3.11 and can build a venv?
py_ok() {
  command -v "$1" >/dev/null 2>&1 || return 1
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)' 2>/dev/null
}

# --- 1. system deps ----------------------------------------------------------
step "Installing system packages (Python >= 3.11, venv, git)"
command -v apt-get >/dev/null 2>&1 || die "this script expects apt (Debian/Ubuntu)"
apt-get update -qq
apt-get install -y git >/dev/null

# Pick an existing suitable interpreter (distro default first — e.g. 3.12 on
# Ubuntu 24.04 — then explicit minor versions).
for cand in python3 python3.13 python3.12 python3.11; do
  if py_ok "$cand"; then PYTHON="$cand"; break; fi
done

if [ -n "$PYTHON" ]; then
  # Ensure venv support for the chosen interpreter (package name tracks its version).
  pyver="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  apt-get install -y "python${pyver}-venv" >/dev/null 2>&1 || apt-get install -y python3-venv >/dev/null
  ok "using $PYTHON ($("$PYTHON" --version 2>&1))"
else
  warn "no Python >= 3.11 found — installing python3.11 from the deadsnakes PPA"
  apt-get install -y software-properties-common >/dev/null
  add-apt-repository -y ppa:deadsnakes/ppa >/dev/null
  apt-get update -qq
  apt-get install -y python3.11 python3.11-venv >/dev/null
  py_ok python3.11 || die "python3.11 install failed"
  PYTHON="python3.11"
  ok "installed $($PYTHON --version 2>&1)"
fi

# --- 2. dedicated user -------------------------------------------------------
step "Ensuring '$APP_USER' system user exists"
if id "$APP_USER" >/dev/null 2>&1; then
  ok "user '$APP_USER' already exists"
else
  useradd --system --create-home --home-dir "$APP_DIR" "$APP_USER"
  ok "created user '$APP_USER'"
fi

# --- 3. code -----------------------------------------------------------------
step "Fetching the code into $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only
  ok "repo updated"
else
  # $APP_DIR is the user's home and may already exist (from useradd) but be empty.
  if [ -n "$(ls -A "$APP_DIR" 2>/dev/null || true)" ]; then
    warn "$APP_DIR is not empty and not a git repo"
    [ "$(ask "Clone into a temp dir and move .git in? (y/N)" "N")" = "y" ] || die "aborting; clear $APP_DIR or clone manually"
    tmp="$(mktemp -d)"
    git clone "$REPO_URL" "$tmp"
    mv "$tmp/.git" "$APP_DIR/.git"
    chown -R "$APP_USER:$APP_USER" "$APP_DIR/.git"
    sudo -u "$APP_USER" git -C "$APP_DIR" checkout -- . || true
    rm -rf "$tmp"
  else
    chown "$APP_USER:$APP_USER" "$APP_DIR"
    sudo -u "$APP_USER" git clone "$REPO_URL" "$APP_DIR"
  fi
  ok "repo cloned"
fi

# --- 4. virtualenv + deps ----------------------------------------------------
step "Setting up the virtualenv and installing dependencies"
if [ ! -x "$APP_DIR/.venv/bin/pip" ]; then
  sudo -u "$APP_USER" "$PYTHON" -m venv "$APP_DIR/.venv"
  ok "created .venv"
fi
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR"
ok "dependencies installed"

# --- 5. config (.env) — the interactive part ---------------------------------
step "Configuring $ENV_FILE"
if [ -f "$ENV_FILE" ]; then
  warn ".env already exists"
  if [ "$(ask "Keep existing .env? (Y/n)" "Y")" = "n" ]; then
    cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"
    rm -f "$ENV_FILE"
    ok "backed up and removed old .env"
  fi
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "I'll ask for the settings that matter. Press Enter to accept defaults."
  OPENAI_KEY=""
  while [ -z "$OPENAI_KEY" ]; do
    OPENAI_KEY="$(ask_secret "OpenAI API key (AMBER_OPENAI_API_KEY, required)")"
    [ -z "$OPENAI_KEY" ] && warn "required — try again"
  done
  AUTH_SECRET="$(ask_secret "WS auth secret (AMBER_AUTH_SECRET, blank = no auth)")"
  PORT="$(ask "Server port (AMBER_PORT)" "8000")"
  LOG_LEVEL="$(ask "Log level (DEBUG/INFO/WARNING)" "INFO")"
  TTS_VOICE="$(ask "TTS voice" "alloy")"

  # Start from the checked-in example so new keys appear automatically, then override.
  sudo -u "$APP_USER" cp "$APP_DIR/.env.example" "$ENV_FILE"
  set_kv() {  # set_kv KEY VALUE — replace or append in $ENV_FILE
    local key="$1" val="$2"
    if grep -qE "^${key}=" "$ENV_FILE"; then
      # use | as sed delimiter; escape any | in the value
      sed -i "s|^${key}=.*|${key}=${val//|/\\|}|" "$ENV_FILE"
    else
      echo "${key}=${val}" >> "$ENV_FILE"
    fi
  }
  set_kv AMBER_OPENAI_API_KEY "$OPENAI_KEY"
  set_kv AMBER_AUTH_SECRET    "$AUTH_SECRET"
  set_kv AMBER_PORT           "$PORT"
  set_kv AMBER_LOG_LEVEL      "$LOG_LEVEL"
  set_kv AMBER_TTS_VOICE      "$TTS_VOICE"

  chown "$APP_USER:$APP_USER" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  ok "wrote $ENV_FILE (chmod 600)"
else
  ok "keeping existing .env"
fi

# --- 6. systemd service ------------------------------------------------------
step "Installing the systemd service"
[ -f "$SERVICE_SRC" ] || die "missing $SERVICE_SRC — is the repo complete?"
install -m 644 "$SERVICE_SRC" "$SERVICE_DST"
systemctl daemon-reload
systemctl enable --now amber
ok "service enabled and started"

# --- 7. verify ---------------------------------------------------------------
step "Verifying"
sleep 2
PORT="$(grep -E '^AMBER_PORT=' "$ENV_FILE" | cut -d= -f2)"; PORT="${PORT:-8000}"
systemctl --no-pager --lines=0 status amber || true
if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  ok "health check passed on port $PORT"
else
  warn "health check failed — inspect logs: journalctl -u amber -e"
fi

echo
ok "Amber is set up."
echo "  logs:    journalctl -u amber -f"
echo "  update:  sudo bash $APP_DIR/deploy/update.sh"
echo "  TLS:     front with nginx/Caddy for wss:// (see deploy/README.md)"
