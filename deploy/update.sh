#!/usr/bin/env bash
#
# Update Amber on the VPS: pull latest, reinstall deps if they changed, restart.
# Idempotent and safe to run repeatedly.
#
# Usage (as root):
#   sudo bash /opt/amber/deploy/update.sh
#
set -euo pipefail

APP_DIR="/opt/amber"
APP_USER="amber"
PIP="$APP_DIR/.venv/bin/pip"

c_blue=$'\033[1;34m'; c_green=$'\033[1;32m'; c_yellow=$'\033[1;33m'; c_red=$'\033[1;31m'; c_off=$'\033[0m'
step() { echo; echo "${c_blue}==>${c_off} $*"; }
ok()   { echo "${c_green} ok${c_off} $*"; }
warn() { echo "${c_yellow} ! ${c_off} $*"; }
die()  { echo "${c_red}error:${c_off} $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (try: sudo bash $0)"
[ -d "$APP_DIR/.git" ] || die "$APP_DIR is not a git checkout — run deploy/setup.sh first"

# Ensure the checkout is owned by amber and git trusts it (avoids "dubious ownership").
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
sudo -u "$APP_USER" git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true

step "Pulling latest from origin"
before="$(sudo -u "$APP_USER" git -C "$APP_DIR" rev-parse HEAD)"
sudo -u "$APP_USER" git -C "$APP_DIR" fetch --quiet origin
sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only
after="$(sudo -u "$APP_USER" git -C "$APP_DIR" rev-parse HEAD)"

if [ "$before" = "$after" ]; then
  ok "already up to date ($after)"
else
  ok "updated $before -> $after"
fi

# Reinstall deps only if dependency-defining files changed (or HEAD didn't move
# but you want to force it: just run `pip install -e .` manually).
step "Checking dependencies"
if [ "$before" != "$after" ] && \
   sudo -u "$APP_USER" git -C "$APP_DIR" diff --name-only "$before" "$after" \
     | grep -qE '^(pyproject\.toml|setup\.cfg|setup\.py|requirements.*\.txt)$'; then
  warn "dependency files changed — reinstalling"
  sudo -u "$APP_USER" "$PIP" install --quiet -e "$APP_DIR"
  ok "dependencies reinstalled"
else
  ok "no dependency changes"
fi

# Reinstall the systemd unit if it changed in this pull.
if [ "$before" != "$after" ] && \
   sudo -u "$APP_USER" git -C "$APP_DIR" diff --name-only "$before" "$after" \
     | grep -qx 'deploy/amber.service'; then
  step "Updating systemd unit"
  install -m 644 "$APP_DIR/deploy/amber.service" /etc/systemd/system/amber.service
  systemctl daemon-reload
  ok "unit reinstalled + daemon reloaded"
fi

step "Restarting Amber"
systemctl restart amber
ok "restarted"

step "Verifying"
sleep 2
PORT="$(grep -E '^AMBER_PORT=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2)"; PORT="${PORT:-8000}"
if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  ok "health check passed on port $PORT"
else
  warn "health check failed — inspect logs: journalctl -u amber -e"
  exit 1
fi

ok "Update complete."
