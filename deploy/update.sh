#!/usr/bin/env bash
#
# Update Amber on the VPS — the ONE command you run to bring a deploy in sync
# with the repo. It reconciles everything, not just the code:
#
#   code      git pull (fast-forward only)
#   python    rebuilds .venv if its interpreter went missing or below 3.11
#   deps      reinstalls when a dependency file changed (or the venv was rebuilt)
#   .env      adds any new keys from .env.example; prompts for missing secrets
#   systemd   reinstalls the unit whenever it differs from the repo's copy
#   restart   restarts the service and health-checks it
#
# Idempotent and safe to run repeatedly. You should never have to hand-edit
# .env or touch systemd after running this.
#
# Usage (as root):
#   sudo bash /opt/amber/deploy/update.sh
#
set -euo pipefail

# App location + the user that owns/runs it. Auto-derived so this works whatever
# the deploy user is (amber, ubuntu, …): APP_DIR is this script's repo root, and
# APP_USER is whoever owns it. Override either with AMBER_APP_DIR / AMBER_APP_USER.
APP_DIR="${AMBER_APP_DIR:-"$(cd "$(dirname "$0")/.." && pwd)"}"
APP_USER="${AMBER_APP_USER:-"$(stat -c '%U' "$APP_DIR")"}"
ENV_FILE="$APP_DIR/.env"
EXAMPLE="$APP_DIR/.env.example"
SERVICE_SRC="$APP_DIR/deploy/amber.service"
SERVICE_DST="/etc/systemd/system/amber.service"
PIP="$APP_DIR/.venv/bin/pip"
VENV_PY="$APP_DIR/.venv/bin/python"

# --- pretty output -----------------------------------------------------------
c_blue=$'\033[1;34m'; c_green=$'\033[1;32m'; c_yellow=$'\033[1;33m'; c_red=$'\033[1;31m'; c_off=$'\033[0m'
step() { echo; echo "${c_blue}==>${c_off} $*"; }
ok()   { echo "${c_green} ok${c_off} $*"; }
warn() { echo "${c_yellow} ! ${c_off} $*"; }
die()  { echo "${c_red}error:${c_off} $*" >&2; exit 1; }

as_user() { sudo -u "$APP_USER" "$@"; }

ask_secret() {  # ask_secret "Prompt"  -> echoes answer on stdout, input hidden
  local prompt="$1" reply
  read -rsp "$prompt: " reply </dev/tty; echo >&2
  echo "$reply"
}

# Is $1 a python that's >= 3.11?
py_ok() {
  command -v "$1" >/dev/null 2>&1 || return 1
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)' 2>/dev/null
}

[ "$(id -u)" -eq 0 ] || die "run as root (try: sudo bash $0)"
[ -d "$APP_DIR/.git" ] || die "$APP_DIR is not a git checkout — run deploy/setup.sh first"

# Ensure the checkout is owned by amber and git trusts it (avoids "dubious ownership").
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
as_user git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true

# --- 1. pull -----------------------------------------------------------------
step "Pulling latest from origin"
before="$(as_user git -C "$APP_DIR" rev-parse HEAD)"
as_user git -C "$APP_DIR" fetch --quiet origin
if ! as_user git -C "$APP_DIR" pull --ff-only --quiet; then
  die "git pull --ff-only failed — the checkout has diverged from origin.
       Inspect: sudo -u $APP_USER git -C $APP_DIR status"
fi
after="$(as_user git -C "$APP_DIR" rev-parse HEAD)"

if [ "$before" = "$after" ]; then
  ok "already up to date ($after)"
else
  ok "updated $before -> $after"
fi

# --- 2. python / venv health -------------------------------------------------
# The OS can upgrade or remove the interpreter the venv was built against,
# leaving .venv/bin/python a dangling symlink. Detect that and rebuild.
step "Checking the virtualenv"
rebuilt=0
if [ ! -x "$VENV_PY" ] || ! py_ok "$VENV_PY"; then
  PY=""
  for cand in python3 python3.13 python3.12 python3.11; do
    if py_ok "$cand"; then PY="$cand"; break; fi
  done
  [ -n "$PY" ] || die "the virtualenv is broken and no system Python >= 3.11 is
       available to rebuild it — install one first (see deploy/setup.sh)"
  warn "virtualenv missing or below 3.11 — rebuilding with $PY"
  rm -rf "$APP_DIR/.venv"
  as_user "$PY" -m venv "$APP_DIR/.venv"
  rebuilt=1
  ok "rebuilt .venv ($("$VENV_PY" --version 2>&1))"
else
  ok "venv healthy ($("$VENV_PY" --version 2>&1))"
fi

# --- 3. dependencies ---------------------------------------------------------
step "Checking dependencies"
dep_changed=0
if [ "$before" != "$after" ] && \
   as_user git -C "$APP_DIR" diff --name-only "$before" "$after" \
     | grep -qE '^(pyproject\.toml|setup\.cfg|setup\.py|requirements.*\.txt)$'; then
  dep_changed=1
fi
if [ "$rebuilt" -eq 1 ] || [ "$dep_changed" -eq 1 ]; then
  [ "$rebuilt" -eq 1 ] && warn "fresh venv — installing deps" || warn "dependency files changed — reinstalling"
  as_user "$PIP" install --quiet --upgrade pip
  as_user "$PIP" install --quiet -e "$APP_DIR"
  ok "dependencies installed"
else
  ok "no dependency changes"
fi

# --- 4. .env reconciliation --------------------------------------------------
# Goal: after this step the live .env has every key the current .env.example
# defines, every required secret is filled, and no existing value is clobbered.
step "Reconciling $ENV_FILE against .env.example"
[ -f "$EXAMPLE" ] || die "missing $EXAMPLE — is the repo complete?"

if [ ! -f "$ENV_FILE" ]; then
  warn ".env missing — creating it from .env.example"
  cp "$EXAMPLE" "$ENV_FILE"
fi

env_has() { grep -qE "^$1=" "$ENV_FILE"; }
env_get() { grep -E "^$1=" "$ENV_FILE" | head -n1 | cut -d= -f2-; }
env_set() {  # env_set KEY VALUE — replace in place or append, never duplicate.
  # Uses awk+ENVIRON so the value is treated as a literal (any character is
  # safe — no sed-delimiter or backslash-escape pitfalls).
  local key="$1" val="$2" tmp
  if env_has "$key"; then
    tmp="$(mktemp)"
    K="$key" V="$val" awk 'BEGIN{k=ENVIRON["K"]; v=ENVIRON["V"]}
      {if (index($0, k"=")==1) print k"="v; else print}' "$ENV_FILE" > "$tmp"
    cat "$tmp" > "$ENV_FILE"; rm -f "$tmp"
  else
    printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
  fi
}

# 4a. Pull in any keys that exist in the example but not in the live file,
#     carrying the example's default value (placeholder secrets included —
#     those get caught and prompted for in 4b).
added=()
while IFS= read -r line; do
  case "$line" in ''|\#*) continue ;; esac
  key=${line%%=*}
  [ "$key" = "$line" ] && continue          # line had no '='
  case "$key" in AMBER_*) ;; *) continue ;; esac
  if ! env_has "$key"; then
    printf '%s\n' "$line" >> "$ENV_FILE"
    added+=("$key")
  fi
done < "$EXAMPLE"
if [ "${#added[@]}" -gt 0 ]; then
  ok "added ${#added[@]} new key(s) with defaults: ${added[*]}"
else
  ok "no new keys in .env.example"
fi

# 4b. Prompt for any secret that is required but still unset. A value "needs"
#     filling if it's a literal example placeholder (contains '...') or — for
#     the always-required keys — empty. Optional empties (e.g. AMBER_AUTH_SECRET)
#     are left alone.
is_placeholder() { case "$1" in *...*) return 0 ;; *) return 1 ;; esac; }
feature_llm="$(env_get AMBER_FEATURE_LLM | tr '[:upper:]' '[:lower:]')"

declare -A LABELS=(
  [AMBER_OPENAI_API_KEY]="OpenAI API key (STT + TTS)"
  [AMBER_ANTHROPIC_API_KEY]="Anthropic API key (the Claude brain)"
)

missing_required=0
prompt_for() {  # prompt_for KEY
  local key="$1" label="${LABELS[$1]:-value for $1}" val=""
  if [ ! -e /dev/tty ]; then
    warn "$key is unset and no terminal is available to prompt — set it in $ENV_FILE"
    missing_required=1
    return
  fi
  while [ -z "$val" ]; do
    val="$(ask_secret "$label [$key]")"
    [ -z "$val" ] && warn "required — try again"
  done
  env_set "$key" "$val"
  ok "set $key"
}

for key in $(grep -oE '^AMBER_[A-Z0-9_]+' "$EXAMPLE" | sort -u); do
  val="$(env_get "$key")"
  need=0
  is_placeholder "$val" && need=1
  if [ -z "$val" ]; then
    case "$key" in
      AMBER_OPENAI_API_KEY) need=1 ;;
      AMBER_ANTHROPIC_API_KEY) [ "$feature_llm" = "false" ] || need=1 ;;
    esac
  fi
  [ "$need" -eq 1 ] && prompt_for "$key"
done

chown "$APP_USER:$APP_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"
[ "$missing_required" -eq 0 ] || die "required secrets are still unset in $ENV_FILE — fill them and re-run"

# --- 5. systemd unit ---------------------------------------------------------
# Render the repo's unit for THIS deploy (substituting the real user + app path),
# then install it only when there isn't one yet. If an existing unit differs we do
# NOT overwrite it: a hand-customized unit (different User, relaxed hardening for
# self-update, etc.) must win over the template — silently reverting it is how a
# working box gets re-broken. Force adopting the template with AMBER_FORCE_UNIT=1.
[ -f "$SERVICE_SRC" ] || die "missing $SERVICE_SRC — is the repo complete?"
rendered="$(mktemp)"
sed -e "s|^User=.*|User=$APP_USER|" \
    -e "s|^Group=.*|Group=$APP_USER|" \
    -e "s|/opt/amber|$APP_DIR|g" \
    "$SERVICE_SRC" > "$rendered"

if [ ! -f "$SERVICE_DST" ]; then
  step "Installing systemd unit (user $APP_USER)"
  install -m 644 "$rendered" "$SERVICE_DST"
  systemctl daemon-reload
  ok "unit installed + daemon reloaded"
elif ! cmp -s "$rendered" "$SERVICE_DST"; then
  if [ "${AMBER_FORCE_UNIT:-0}" = "1" ]; then
    step "Updating systemd unit (forced)"
    install -m 644 "$rendered" "$SERVICE_DST"
    systemctl daemon-reload
    ok "unit reinstalled + daemon reloaded"
  else
    warn "installed unit differs from the repo template — leaving it untouched"
    warn "  (looks customized for this box; adopt the template with AMBER_FORCE_UNIT=1)"
  fi
fi
rm -f "$rendered"

# --- 6. restart + verify -----------------------------------------------------
step "Restarting Amber"
systemctl restart amber
ok "restarted"

step "Verifying"
sleep 2
PORT="$(env_get AMBER_PORT)"; PORT="${PORT:-8000}"
if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  ok "health check passed on port $PORT"
else
  warn "health check failed — inspect logs: journalctl -u amber -e"
  exit 1
fi

ok "Update complete."
