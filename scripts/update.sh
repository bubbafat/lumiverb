#!/usr/bin/env bash
# Update this Lumiverb install in one go: the code, then the API (packages,
# migrations, units, restarts; update-api.sh), then the web UI (update-web.sh).
#
#   sudo bash /opt/lumiverb/scripts/update.sh [--branch NAME]
#
# --branch moves the install to another branch, remembered for the next
# update and for deploy-api.sh. Without it, the checkout's branch is updated.
#
# Everything goes to the screen and to /var/log/lumiverb/update-<time>.log;
# update-latest.log is the newest. The log belongs to whoever ran sudo, so
# it can be read without sudo. Its last line says "Result: OK" or
# "Result: FAILED".
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

APP_DIR="${LUMIVERB_APP_DIR:-/opt/lumiverb}"
ENV_FILE="${LUMIVERB_CONF_DIR:-/etc/lumiverb}/env"
LOG_DIR="${LUMIVERB_LOG_DIR:-/var/log/lumiverb}"
NGINX_SITE="${LUMIVERB_NGINX_SITE:-/etc/nginx/sites-available/lumiverb}"
SVC_USER="lumiverb"
KEEP_LOGS=20

BRANCH="${LUMIVERB_UPDATE_BRANCH:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch) BRANCH="${2:?Missing value for --branch}"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) fail "Unknown option: $1 (see --help)" ;;
  esac
done

[[ "$(id -u)" -eq 0 ]] || fail "Run it with sudo: sudo bash $0"
[[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR isn't a git checkout; install with deploy-api.sh first"

# The repo belongs to the service user: git runs as it.
as_svc() { sudo -u "$SVC_USER" "$@"; }

# The first run starts the log; the pulled copy it hands over to (below)
# writes on to the same one.
if [[ -z "${LUMIVERB_UPDATE_LOG:-}" ]]; then
  mkdir -p "$LOG_DIR"
  LUMIVERB_UPDATE_LOG="$LOG_DIR/update-$(date +%Y%m%d-%H%M%S)-$$.log"
  : > "$LUMIVERB_UPDATE_LOG"
  if [[ -n "${SUDO_USER:-}" ]]; then
    chown "$SUDO_USER" "$LUMIVERB_UPDATE_LOG"
    chmod 640 "$LUMIVERB_UPDATE_LOG"
  else
    chmod 644 "$LUMIVERB_UPDATE_LOG"
  fi
  ln -sfn "$(basename "$LUMIVERB_UPDATE_LOG")" "$LOG_DIR/update-latest.log"
  # shellcheck disable=SC2012 # names are ours: update-<date>-<time>-<pid>.log
  ls -1t "$LOG_DIR"/update-2*.log 2>/dev/null | tail -n +$((KEEP_LOGS + 1)) | xargs -r rm -f || true
  export LUMIVERB_UPDATE_LOG
  exec > >(tee -a "$LUMIVERB_UPDATE_LOG") 2>&1
  echo "Lumiverb update on $(hostname) at $(date -Is), run by ${SUDO_USER:-root}${BRANCH:+, --branch $BRANCH}"
  echo "Log: $LUMIVERB_UPDATE_LOG"
fi

finish() {
  local rc=$?
  if [[ $rc -eq 0 ]]; then
    echo -e "\n${GREEN}${BOLD}Result: OK${NC}"
  else
    echo -e "\n${RED}${BOLD}Result: FAILED${NC} (exit ${rc}): the step above says where it stopped. Fix that and run the update again."
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
  as_svc git fetch --prune origin
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
  exec bash "$APP_DIR/scripts/update.sh"
fi

# ---------------------------------------------------------------------------
step "Remembering the branch"
if [[ -f "$ENV_FILE" ]]; then
  if grep -q '^BRANCH=' "$ENV_FILE"; then
    sed -i "s|^BRANCH=.*|BRANCH=${BRANCH}|" "$ENV_FILE"
  else
    [[ ! -s "$ENV_FILE" || -z "$(tail -c1 "$ENV_FILE")" ]] || echo >> "$ENV_FILE"
    echo "BRANCH=${BRANCH}" >> "$ENV_FILE"
  fi
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
if [[ "$AFTER" == "$LUMIVERB_UPDATE_BEFORE" ]]; then
  echo "  No new commits"
else
  echo "  New commits ($(as_svc git rev-list --count "${LUMIVERB_UPDATE_BEFORE}..${AFTER}"), newest first):"
  as_svc git log --max-count=30 --format='    %h %s' "${LUMIVERB_UPDATE_BEFORE}..${AFTER}"
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
if curl -sf "http://127.0.0.1:${API_PORT}/health" >/dev/null; then
  ok "API answers on port ${API_PORT}"
else
  fail "API doesn't answer on port ${API_PORT}: journalctl -u lumiverb-api -n 50"
fi
[[ ${#DOWN[@]} -eq 0 ]] || fail "Not running: ${DOWN[*]} (journalctl -u <name> -n 50)"
