#!/usr/bin/env bash
# Update the Lumiverb web UI on an existing deployment.
#
# Usage (from web server):
#   bash /opt/lumiverb/scripts/update-web.sh
#
# Or remote:
#   ssh root@your-web-server 'bash /opt/lumiverb/scripts/update-web.sh'
#
# What it does:
#   1. git pull
#   2. npm ci + npm run build
#   3. nginx -s reload
#
# Sub-10-second updates. Does NOT touch Python, migrations, or API services.
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
SVC_USER="lumiverb"

[[ "$(id -u)" -eq 0 ]] || fail "Run as root"
[[ -d "${APP_DIR}/.git" ]] || fail "${APP_DIR} is not a git repo — run deploy-web.sh first"

# Repo is owned by $SVC_USER; tell git it's safe for root.
git config --system --replace-all safe.directory "$APP_DIR" "$APP_DIR" 2>/dev/null \
  || git config --global --add safe.directory "$APP_DIR"

cd "$APP_DIR"

# ---------------------------------------------------------------------------
step "Pulling latest code"
sudo -u "$SVC_USER" git fetch --all --prune

if sudo -u "$SVC_USER" git symbolic-ref -q HEAD >/dev/null 2>&1; then
  sudo -u "$SVC_USER" git pull
else
  warn "Detached HEAD detected — skipping git pull"
fi
ok "$(sudo -u "$SVC_USER" git log --oneline -1)"

# ---------------------------------------------------------------------------
step "Rebuilding web UI"
cd "$APP_DIR/src/ui/web"
sudo -u "$SVC_USER" npm ci --no-audit --no-fund
sudo -u "$SVC_USER" npm run build
ok "Web UI built"
cd "$APP_DIR"

# ---------------------------------------------------------------------------
step "Reloading nginx"
nginx -t || fail "nginx config test failed"
nginx -s reload
ok "nginx reloaded"

# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}Web update complete.${NC}"
