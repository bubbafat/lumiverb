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

# Ensure the service user's HOME exists.
SVC_HOME="$(getent passwd "$SVC_USER" | cut -d: -f6)"
if [[ -n "$SVC_HOME" ]] && [[ ! -d "$SVC_HOME" ]]; then
  mkdir -p "$SVC_HOME"
  chown "$SVC_USER":"$SVC_USER" "$SVC_HOME"
fi

# Repo is owned by $SVC_USER; tell git it's safe for root.
git config --system --replace-all safe.directory "$APP_DIR" "$APP_DIR" 2>/dev/null \
  || git config --global --add safe.directory "$APP_DIR"

cd "$APP_DIR"

# ---------------------------------------------------------------------------
step "Pulling latest code"
sudo -u "$SVC_USER" git fetch --all --prune

# If HEAD is detached (e.g. pinned to a tag/commit), skip pull — user must
# explicitly checkout the desired ref before running this script.
if sudo -u "$SVC_USER" git symbolic-ref -q HEAD >/dev/null 2>&1; then
  sudo -u "$SVC_USER" git pull
else
  warn "Detached HEAD detected — skipping git pull (checkout a branch or tag first to change versions)"
fi
ok "$(sudo -u "$SVC_USER" git log --oneline -1)"

# ---------------------------------------------------------------------------
step "Updating Python dependencies"
sudo -u "$SVC_USER" "$UV_BIN" sync --extra cli
ok "Python venv synced (server + cli)"

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
step "Ensuring data directory"
DATA_DIR="$(grep '^DATA_DIR=' "$ENV_FILE" | cut -d= -f2-)"
if [[ -n "$DATA_DIR" ]]; then
  mkdir -p "$DATA_DIR"/quickwit
  chown -R "$SVC_USER":"$SVC_USER" "$DATA_DIR"
  ok "Data dir: $DATA_DIR"
fi

# ---------------------------------------------------------------------------
step "Fixing Quickwit sandbox (namespace-dependent directives)"
# PrivateTmp, ProtectSystem, ReadWritePaths, ReadOnlyPaths require mount
# namespaces which many VPS hosts block (status=226/NAMESPACE).  Strip them
# from the base unit file directly.
QW_UNIT="/etc/systemd/system/lumiverb-quickwit.service"
if [[ -f "$QW_UNIT" ]] && grep -qE '^(PrivateTmp|ProtectSystem|ReadWritePaths|ReadOnlyPaths)=' "$QW_UNIT"; then
  sed -i '/^PrivateTmp=/d; /^ProtectSystem=/d; /^ReadWritePaths=/d; /^ReadOnlyPaths=/d' "$QW_UNIT"
  systemctl daemon-reload
  ok "Removed namespace-dependent directives from Quickwit unit"
fi

# ---------------------------------------------------------------------------
step "Installing upkeep timers"
cat > /etc/systemd/system/lumiverb-upkeep.service <<UPKEEP_SVC
[Unit]
Description=Lumiverb periodic upkeep (search sync, cleanup)

[Service]
Type=oneshot
User=${SVC_USER}
Group=${SVC_USER}
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/curl -sf -X POST http://127.0.0.1:8000/v1/upkeep -H "Authorization: Bearer \${ADMIN_KEY}" -H "Content-Type: application/json"
TimeoutSec=120
UPKEEP_SVC

cat > /etc/systemd/system/lumiverb-upkeep.timer <<UPKEEP_TMR
[Unit]
Description=Run Lumiverb upkeep every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=30s

[Install]
WantedBy=timers.target
UPKEEP_TMR

cat > /etc/systemd/system/lumiverb-upkeep-daily.service <<DAILY_SVC
[Unit]
Description=Lumiverb daily maintenance (filesystem cleanup)

[Service]
Type=oneshot
User=${SVC_USER}
Group=${SVC_USER}
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/curl -sf -X POST "http://127.0.0.1:8000/v1/upkeep/cleanup?dry_run=false" -H "Authorization: Bearer \${ADMIN_KEY}" -H "Content-Type: application/json"
TimeoutSec=300
DAILY_SVC

cat > /etc/systemd/system/lumiverb-upkeep-daily.timer <<DAILY_TMR
[Unit]
Description=Run Lumiverb daily maintenance at 3am

[Timer]
OnCalendar=*-*-* 03:00:00
AccuracySec=5min
Persistent=true

[Install]
WantedBy=timers.target
DAILY_TMR

systemctl daemon-reload
systemctl enable --now lumiverb-upkeep.timer lumiverb-upkeep-daily.timer
ok "Upkeep timers installed and started"

# ---------------------------------------------------------------------------
step "Restarting services"
systemctl restart lumiverb-api
# Only restart worker and quickwit if they are enabled
systemctl is-enabled lumiverb-worker >/dev/null 2>&1 && systemctl restart lumiverb-worker
systemctl is-enabled lumiverb-quickwit >/dev/null 2>&1 && systemctl restart lumiverb-quickwit

for i in {1..10}; do
  if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

systemctl status --no-pager lumiverb-api || true
systemctl is-enabled lumiverb-quickwit >/dev/null 2>&1 && systemctl status --no-pager lumiverb-quickwit || true
systemctl is-enabled lumiverb-worker >/dev/null 2>&1 && systemctl status --no-pager lumiverb-worker || true

if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
  ok "API server healthy"
else
  fail "API server not responding — check: journalctl -u lumiverb-api -n 50"
fi

echo ""
echo -e "${GREEN}${BOLD}Update complete.${NC}"
