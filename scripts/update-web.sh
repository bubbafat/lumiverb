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
#   3. Add nginx locations deploy-web.sh has gained since (playback streams)
#   4. nginx -s reload
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
# set -e stops at the first failed command; say which, so a stop is never silent.
trap 'echo -e "${RED}  ✗ Stopped at line ${LINENO}: ${BASH_COMMAND}${NC}" >&2' ERR

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

# bash keeps running the copy of this script it started with, and git pull
# wrote a new one: run that, so steps this update adds apply now.
if [[ "${LUMIVERB_UPDATE_REEXEC:-}" != "1" ]]; then
  export LUMIVERB_UPDATE_REEXEC=1
  exec bash "$APP_DIR/scripts/update-web.sh" "$@"
fi

# ---------------------------------------------------------------------------
step "Rebuilding web UI"
cd "$APP_DIR/src/ui/web"
sudo -u "$SVC_USER" npm ci --no-audit --no-fund
sudo -u "$SVC_USER" npm run build
ok "Web UI built"
cd "$APP_DIR"

# ---------------------------------------------------------------------------
step "Updating the nginx site"
SITE="/etc/nginx/sites-available/lumiverb"
# Sites deploy-web.sh wrote before playback streams had their own location
# get it here, once, with the upstream of their /v1/ location.
if [[ -f "$SITE" ]] && ! grep -q "location /v1/stream/ {" "$SITE"; then
  UPSTREAM="$(awk '$1 == "location" && $2 == "/v1/" { f = 1 } f && $1 == "proxy_pass" { sub(/;$/, "", $2); print $2; exit }' "$SITE")"
  if [[ -z "$UPSTREAM" ]]; then
    warn "No /v1/ location in ${SITE}; playback streams stay buffered (see deploy-web.sh)"
  else
    STREAM_LOCATION="$(cat <<NGINX
    # Playback: a multi-GB proxy streams straight through instead of being
    # spooled to disk, and the link (a bearer token good for hours) stays
    # out of the access log. Range requests pass through as they are.
    location /v1/stream/ {
        proxy_pass ${UPSTREAM};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_buffering off;
        proxy_request_buffering off;
        access_log off;
        proxy_connect_timeout 60s;
        proxy_read_timeout 300s;
    }
NGINX
)"
    STREAM_LOCATION="$STREAM_LOCATION" awk '
      $1 == "location" && $2 == "/v1/" && !done { print ENVIRON["STREAM_LOCATION"]; print ""; done = 1 }
      { print }
    ' "$SITE" > "${SITE}.new"
    mv "${SITE}.new" "$SITE"
    ok "Playback streams unbuffered and out of nginx's access log"
  fi
fi

# ---------------------------------------------------------------------------
step "Reloading nginx"
nginx -t || fail "nginx config test failed"
nginx -s reload
ok "nginx reloaded"

# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}Web update complete.${NC}"
