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
#   3. Give the nginx site what deploy-web.sh's has gained since (playback
#      streams, cache headers for the page and the built files, CSP, HSTS on
#      HTTPS, and src/server/lifecycle/site_update.py's: headers, compression,
#      404s)
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

# What root runs as the service user reads none of that user's own settings: git's (~/.gitconfig, which can name
# hooks and an fsmonitor to run) and npm's (~/.npmrc). They're the service user's to write, not root's to follow.
# Its home and caches stay as they were.
as_svc() { sudo -u "$SVC_USER" env GIT_CONFIG_GLOBAL=/dev/null npm_config_userconfig=/dev/null "$@"; }

# The repo is the service user's, and git runs in it only as that user. Earlier versions marked it safe for root
# (safe.directory), which let its own settings (.git/config) steer a git root ran there: unmarked.
git config --system --fixed-value --unset-all safe.directory "$APP_DIR" 2>/dev/null || true
git config --global --fixed-value --unset-all safe.directory "$APP_DIR" 2>/dev/null || true

cd "$APP_DIR"

# ---------------------------------------------------------------------------
step "Pulling latest code"
as_svc git fetch --all --prune

if as_svc git symbolic-ref -q HEAD >/dev/null 2>&1; then
  as_svc git pull
else
  warn "Detached HEAD detected — skipping git pull"
fi
ok "$(as_svc git log --oneline -1)"

# bash keeps running the copy of this script it started with, and git pull
# wrote a new one: run that, so steps this update adds apply now.
if [[ "${LUMIVERB_UPDATE_REEXEC:-}" != "1" ]]; then
  export LUMIVERB_UPDATE_REEXEC=1
  exec bash "$APP_DIR/scripts/update-web.sh" "$@"
fi

# ---------------------------------------------------------------------------
step "Rebuilding web UI"
cd "$APP_DIR/src/ui/web"
as_svc npm ci --no-audit --no-fund
as_svc npm run build
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

# Sites from before the page and the built files had cache headers get them
# here, once, ahead of the SPA's location /.
if [[ -f "$SITE" ]] && ! grep -q "location = /index.html {" "$SITE"; then
  if ! grep -qE '^[[:space:]]*location / \{' "$SITE"; then
    warn "No location / in ${SITE}; the page keeps no cache headers (see deploy-web.sh)"
  else
    CACHE_LOCATIONS="$(cat <<'NGINX'
    # Built files carry a content hash in their name: cache them for good.
    # A location's add_header drops the server's, so they are repeated.
    location /assets/ {
        add_header Cache-Control "public, max-age=31536000, immutable";
        add_header X-Content-Type-Options nosniff always;
        add_header X-Frame-Options DENY always;
        add_header Referrer-Policy no-referrer-when-downgrade always;
    }

    # The page names the build's files, so the browser checks it every time
    # (the SPA fallback below ends here too).
    location = /index.html {
        add_header Cache-Control "no-cache";
        add_header X-Content-Type-Options nosniff always;
        add_header X-Frame-Options DENY always;
        add_header Referrer-Policy no-referrer-when-downgrade always;
    }
NGINX
)"
    CACHE_LOCATIONS="$CACHE_LOCATIONS" awk '
      $1 == "location" && $2 == "/" && $3 == "{" && !done { print ENVIRON["CACHE_LOCATIONS"]; print ""; done = 1 }
      { print }
    ' "$SITE" > "${SITE}.new"
    mv "${SITE}.new" "$SITE"
    ok "The page is checked on every load; built files are cached for good"
  fi
fi

# Sites from before the CSP get it, once, beside each nosniff header (the
# server's and each location's). Same policy as deploy-web.sh.
CSP="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src 'self' blob:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
if [[ -f "$SITE" ]] && ! grep -q "Content-Security-Policy" "$SITE"; then
  CSP="$CSP" awk '
    { print }
    /add_header X-Content-Type-Options/ {
      pad = $0; sub(/add_header.*/, "", pad)
      print pad "add_header Content-Security-Policy \"" ENVIRON["CSP"] "\" always;"
    }
  ' "$SITE" > "${SITE}.new"
  mv "${SITE}.new" "$SITE"
  ok "CSP on"
fi

# HTTPS sites without HSTS get it, once, in the blocks that listen on 443.
if [[ -f "$SITE" ]] && ! grep -q "Strict-Transport-Security" "$SITE" \
    && grep -qE '^[[:space:]]*listen[[:space:]]+(\[::\]:)?443' "$SITE"; then
  awk '
    function flush(   i, pad) {
      for (i = 1; i <= n; i++) {
        print buf[i]
        if (tls && buf[i] ~ /add_header X-Content-Type-Options/) {
          pad = buf[i]; sub(/add_header.*/, "", pad)
          print pad "add_header Strict-Transport-Security \"max-age=31536000\" always;"
        }
      }
      n = 0; tls = 0
    }
    /^server[[:space:]]*\{/ { flush(); inblock = 1 }
    { if (inblock) { buf[++n] = $0; if ($1 == "listen" && $2 ~ /(^|:)443;?$/) tls = 1 } else print }
    /^\}/ && inblock { flush(); inblock = 0 }
    END { flush() }
  ' "$SITE" > "${SITE}.new"
  mv "${SITE}.new" "$SITE"
  ok "HSTS on"
fi

# The rest of what deploy-web.sh's site has gained, as a Mac's run gives its own
# config: the standard library alone (this machine may have no Lumiverb Python).
python3 -I "$APP_DIR/src/server/lifecycle/site_update.py" "$SITE"

# ---------------------------------------------------------------------------
step "Reloading nginx"
nginx -t || fail "nginx config test failed"
nginx -s reload
ok "nginx reloaded"

# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}Web update complete.${NC}"
