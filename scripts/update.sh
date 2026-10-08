#!/usr/bin/env bash
# Update this Lumiverb install in one go: the code, then the API (packages,
# migrations, units, restarts; update-api.sh), then the web UI (update-web.sh).
#
#   sudo bash /opt/lumiverb/scripts/update.sh [--branch NAME]
#
# --branch moves the install to another branch, remembered for the next
# update and for deploy-api.sh. Without it, the checkout's branch is updated.
#
# Everything goes to the screen and to /var/log/lumiverb/update-<date>-<time>-<pid>.log;
# update-latest.log is the newest. The log belongs to whoever ran sudo, so it
# can be read without sudo. It ends with a line "Result: OK" or
# "Result: FAILED". One update runs at a time.
set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

step()  { echo -e "\n${BOLD}=== $1 ===${NC}"; }
ok()    { echo -e "${GREEN}  ✓${NC} $1"; }
warn()  { echo -e "${YELLOW}  ⚠${NC} $1"; }
fail()  { echo -e "${RED}  ✗ $1${NC}" >&2; exit 1; }
# set -e stops at the first failed command; say which, so a stop is never silent.
trap 'echo -e "${RED}  ✗ Stopped at line ${LINENO}: ${BASH_COMMAND}${NC}" >&2' ERR
# Ctrl-C, a kill or a dropped SSH session is a failed update, never an OK one.
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

APP_DIR="${LUMIVERB_APP_DIR:-/opt/lumiverb}"
ENV_FILE="${LUMIVERB_CONF_DIR:-/etc/lumiverb}/env"
LOG_DIR="${LUMIVERB_LOG_DIR:-/var/log/lumiverb}"
NGINX_SITE="${LUMIVERB_NGINX_SITE:-/etc/nginx/sites-available/lumiverb}"
LOCK="${LUMIVERB_UPDATE_LOCK:-/run/lumiverb-update.lock}"
SETTLE="${LUMIVERB_UPDATE_SETTLE:-5}"  # seconds the services must stay up after restarting
SVC_USER="lumiverb"
KEEP_LOGS=20

BRANCH="${LUMIVERB_UPDATE_BRANCH:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch) BRANCH="${2:?Missing value for --branch}"; shift 2 ;;
    -h|--help)
      echo "Usage: sudo bash ${APP_DIR}/scripts/update.sh [--branch NAME]"
      echo "Updates the code, the API and the web UI; logs to ${LOG_DIR}/update-latest.log."
      exit 0 ;;
    *) fail "Unknown option: $1 (see --help)" ;;
  esac
done

[[ "$(id -u)" -eq 0 ]] || fail "Run it with sudo: sudo bash ${APP_DIR}/scripts/update.sh"
[[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR isn't a git checkout; install with deploy-api.sh first"

# The repo belongs to the service user: git runs as it. Never on our stdin,
# which is the script itself when it's piped in (`... | sudo bash -s`).
as_svc() { sudo -u "$SVC_USER" "$@" </dev/null; }

# The first run takes the lock and starts the log; the pulled copy it hands
# over to (below) inherits both.
if [[ -z "${LUMIVERB_UPDATE_LOG:-}" ]]; then
  exec 9>>"$LOCK"
  flock -n 9 || fail "Another update is already running (it holds ${LOCK}); wait for it to finish"

  mkdir -p "$LOG_DIR"
  chmod 755 "$LOG_DIR"
  LUMIVERB_UPDATE_LOG="$LOG_DIR/update-$(date +%Y%m%d-%H%M%S)-$$.log"
  (umask 077; : > "$LUMIVERB_UPDATE_LOG")
  if [[ -n "${SUDO_USER:-}" ]]; then
    chown -h "$SUDO_USER" "$LUMIVERB_UPDATE_LOG"
    chmod 640 "$LUMIVERB_UPDATE_LOG"
  fi
  ln -sfn "$(basename "$LUMIVERB_UPDATE_LOG")" "$LOG_DIR/update-latest.log"
  # shellcheck disable=SC2012 # names are ours: update-<date>-<time>-<pid>.log
  ls -1t "$LOG_DIR"/update-2*.log 2>/dev/null | tail -n +$((KEEP_LOGS + 1)) | xargs -r -d '\n' rm -f -- || true
  export LUMIVERB_UPDATE_LOG
  # tee outlives a Ctrl-C or kill aimed at the whole update, so the log gets its
  # Result. bash won't ignore INT in it, hence tee -i.
  exec > >(trap '' TERM HUP; exec tee -i -a "$LUMIVERB_UPDATE_LOG") 2>&1
  echo "Lumiverb update on $(hostname) at $(date -Is), run by ${SUDO_USER:-root}${BRANCH:+, --branch $BRANCH}"
  echo "Log: $LUMIVERB_UPDATE_LOG"
fi

DONE=""
finish() {
  local rc=$?
  echo
  if [[ $rc -eq 0 && "$DONE" == 1 ]]; then
    echo "Result: OK"
  else
    echo "Result: FAILED"
    echo "(exit ${rc}) The step above says where it stopped. Fix that and run the update again."
  fi
}
trap finish EXIT

cd "$APP_DIR"

# ---------------------------------------------------------------------------
if [[ -z "${LUMIVERB_UPDATE_BEFORE:-}" ]]; then
  step "Code"
  LUMIVERB_UPDATE_BEFORE="$(as_svc git rev-parse HEAD)"
  CURRENT="$(as_svc git symbolic-ref -q --short HEAD || true)"
  BRANCH="${BRANCH:-$CURRENT}"
  [[ -n "$BRANCH" ]] || fail "The checkout isn't on a branch: say which with --branch NAME"
  as_svc git check-ref-format --branch "$BRANCH" >/dev/null 2>&1 || fail "Not a branch name: ${BRANCH}"
  as_svc git fetch --prune origin
  as_svc git rev-parse -q --verify "refs/remotes/origin/${BRANCH}" >/dev/null || fail "origin has no branch ${BRANCH}"
  # What runs next is that branch's update.sh: one from before it existed
  # would leave the install half moved.
  as_svc git cat-file -e "refs/remotes/origin/${BRANCH}:scripts/update.sh" 2>/dev/null \
    || fail "${BRANCH} has no scripts/update.sh (it's older than it): merge main into it first. Nothing was changed."
  if [[ "$BRANCH" != "$CURRENT" ]]; then
    as_svc git checkout "$BRANCH"
    ok "Moved from ${CURRENT:-a detached checkout} to ${BRANCH}"
  fi
  # Never merge on the server: a checkout with commits of its own stops here.
  as_svc git pull --ff-only origin "$BRANCH"
  ok "$(as_svc git log -1 --format='%h %s')"

  # bash keeps running the copy of this script it started with, and the pull
  # may have brought a new one: carry on in that, so steps it adds run now.
  export LUMIVERB_UPDATE_BEFORE LUMIVERB_UPDATE_BRANCH="$BRANCH"
  exec bash "$APP_DIR/scripts/update.sh" </dev/null
fi

# ---------------------------------------------------------------------------
step "Remembering the branch"
if [[ -f "$ENV_FILE" ]]; then
  tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
  # Written whole by awk, not sed: a branch name may hold & or |.
  BRANCH="$BRANCH" awk '
    /^BRANCH=/ { if (!done) print "BRANCH=" ENVIRON["BRANCH"]; done = 1; next }
    { print }
    END { if (!done) print "BRANCH=" ENVIRON["BRANCH"] }
  ' "$ENV_FILE" > "$tmp"
  cat "$tmp" > "$ENV_FILE"  # keeps the file's owner and mode
  rm -f "$tmp"
  ok "${BRANCH} (for the next update and for deploy-api.sh)"
else
  warn "No ${ENV_FILE}: nothing to remember it in"
fi

# ---------------------------------------------------------------------------
step "API, database, worker (update-api.sh)"
bash "$APP_DIR/scripts/update-api.sh"

# ---------------------------------------------------------------------------
if [[ -f "$NGINX_SITE" ]]; then
  step "Web UI (update-web.sh)"
  bash "$APP_DIR/scripts/update-web.sh"
else
  step "Web UI"
  warn "No nginx site at ${NGINX_SITE}: skipped (deploy-web.sh installs it)"
fi

# ---------------------------------------------------------------------------
step "Summary"
AFTER="$(as_svc git rev-parse HEAD)"
echo "  Branch: ${BRANCH}"
echo "  Before: $(as_svc git log -1 --format='%h %s' "$LUMIVERB_UPDATE_BEFORE")"
echo "  After:  $(as_svc git log -1 --format='%h %s' "$AFTER")"
NEW="$(as_svc git rev-list --count "${LUMIVERB_UPDATE_BEFORE}..${AFTER}")"
if [[ "$AFTER" == "$LUMIVERB_UPDATE_BEFORE" ]]; then
  echo "  No new commits"
elif [[ "$NEW" -eq 0 ]]; then
  echo "  No new commits: moved to an older one, $(as_svc git rev-list --count "${AFTER}..${LUMIVERB_UPDATE_BEFORE}") back"
else
  echo "  New commits (${NEW}, newest first):"
  as_svc git log --max-count=30 --format='    %h %s' "${LUMIVERB_UPDATE_BEFORE}..${AFTER}"
fi

# A service that crashes on start can look active for a moment: give it time.
if [[ "$SETTLE" != 0 ]]; then
  echo "  Checking the services in ${SETTLE} s..."
  sleep "$SETTLE"
fi
DOWN=()
for unit in lumiverb-api lumiverb-worker lumiverb-quickwit nginx; do
  systemctl is-enabled --quiet "$unit" 2>/dev/null || continue
  state="$(systemctl is-active "$unit" 2>/dev/null || true)"
  echo "  ${unit}: ${state}"
  [[ "$state" == active ]] || DOWN+=("$unit")
done
API_PORT="$(grep '^API_PORT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
API_PORT="${API_PORT:-8000}"
if curl -sf --max-time 10 "http://127.0.0.1:${API_PORT}/health" >/dev/null; then
  ok "API answers on port ${API_PORT}"
else
  fail "API doesn't answer on port ${API_PORT}: journalctl -u lumiverb-api -n 50"
fi
[[ ${#DOWN[@]} -eq 0 ]] || fail "Not running: ${DOWN[*]} (journalctl -u <name> -n 50)"
DONE=1
