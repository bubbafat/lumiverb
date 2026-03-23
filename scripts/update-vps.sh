#!/usr/bin/env bash
# Update an existing Lumiverb VPS deployment.
#
# Usage (from VPS):
#   bash /opt/lumiverb/scripts/update-vps.sh
#
# Or remote:
#   ssh root@your-vps 'bash /opt/lumiverb/scripts/update-vps.sh'
#
# What it does:
#   1. git pull
#   2. uv sync
#   3. Run migrations
#   4. Rebuild web UI
#   5. Restart services
#
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

APP_DIR="/opt/lumiverb"
ENV_FILE="/etc/lumiverb/env"
SVC_USER="lumiverb"
UV_BIN="/usr/local/bin/uv"

[[ "$(id -u)" -eq 0 ]] || fail "Run as root"
[[ -d "${APP_DIR}/.git" ]] || fail "${APP_DIR} is not a git repo — run deploy-vps.sh first"

cd "$APP_DIR"

# ---------------------------------------------------------------------------
step "Pulling latest code"
sudo -u "$SVC_USER" git fetch --all --prune

# If HEAD is detached (e.g. pinned to a tag/commit), skip pull — user must
# explicitly checkout the desired ref before running this script.
if git symbolic-ref -q HEAD >/dev/null 2>&1; then
  sudo -u "$SVC_USER" git pull
else
  warn "Detached HEAD detected — skipping git pull (checkout a branch or tag first to change versions)"
fi
ok "$(git log --oneline -1)"

# ---------------------------------------------------------------------------
step "Updating Python dependencies"
sudo -u "$SVC_USER" "$UV_BIN" sync --all-extras
ok "Python venv synced"

# ---------------------------------------------------------------------------
step "Running migrations"

# Read DB URL from env file
DB_URL="$(grep '^CONTROL_PLANE_DATABASE_URL=' "$ENV_FILE" | cut -d= -f2-)"
[[ -n "$DB_URL" ]] || fail "CONTROL_PLANE_DATABASE_URL not found in ${ENV_FILE}"

export ALEMBIC_CONTROL_URL="$DB_URL"
sudo -u "$SVC_USER" --preserve-env=ALEMBIC_CONTROL_URL \
  "$APP_DIR/.venv/bin/python" -m alembic -c alembic-control.ini upgrade head
ok "Control plane migrations applied"

# Tenant migrations
export CONTROL_PLANE_DATABASE_URL="$DB_URL"
export UV_BIN
sudo -u "$SVC_USER" --preserve-env=CONTROL_PLANE_DATABASE_URL,ALEMBIC_CONTROL_URL,UV_BIN \
  bash "$APP_DIR/scripts/migrate.sh"
ok "Tenant migrations applied"

# ---------------------------------------------------------------------------
step "Rebuilding web UI"
cd "$APP_DIR/src/ui/web"
sudo -u "$SVC_USER" npm ci --no-audit --no-fund
sudo -u "$SVC_USER" npm run build
ok "Web UI built"
cd "$APP_DIR"

# ---------------------------------------------------------------------------
step "Restarting services"
systemctl restart lumiverb-api lumiverb-worker

for i in {1..10}; do
  if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

systemctl status --no-pager lumiverb-api lumiverb-worker lumiverb-quickwit || true

if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
  ok "API server healthy"
else
  fail "API server not responding — check: journalctl -u lumiverb-api -n 50"
fi

echo ""
echo -e "${GREEN}${BOLD}Update complete.${NC}"
