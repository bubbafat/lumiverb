#!/usr/bin/env bash
# Bootstrap the Lumiverb web UI on a fresh Ubuntu 22.04+ machine.
#
# Installs nginx + Node.js, clones the repo, builds the SPA, configures
# nginx to proxy /v1/* to the API server. Does NOT install Python,
# PostgreSQL, or any API server components.
#
# Usage:
#   bash scripts/deploy-web.sh --domain app.example.com --api-upstream http://10.0.0.5:8000
#
# On a LAN/Tailscale box (like prod), answer on any address over HTTP and
# leave the firewall alone:
#   sudo bash scripts/deploy-web.sh --domain _ --api-upstream http://127.0.0.1:8100 \
#     --no-firewall
#
# Idempotent: safe to run again to update an existing install.
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
DOMAIN=""
API_UPSTREAM=""
REPO_URL="https://github.com/bubbafat/lumiverb.git"
BRANCH=""
CERTBOT_EMAIL=""
SKIP_CERTBOT=false
CERTIFICATE_ARCHIVE=""
NO_FIREWALL=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain)         DOMAIN="${2:?Missing value for --domain}"; shift 2 ;;
    --api-upstream)   API_UPSTREAM="${2:?Missing value for --api-upstream}"; shift 2 ;;
    --repo)           REPO_URL="${2:?Missing value for --repo}"; shift 2 ;;
    --branch)         BRANCH="${2:?Missing value for --branch}"; shift 2 ;;
    --email)          CERTBOT_EMAIL="${2:?Missing value for --email}"; shift 2 ;;
    --certificate)    CERTIFICATE_ARCHIVE="${2:?Missing value for --certificate}"; shift 2 ;;
    --skip-certbot)   SKIP_CERTBOT=true; shift ;;
    --no-firewall)    NO_FIREWALL=true; shift ;;
    --firewall)       NO_FIREWALL=false; shift ;;
    --dry-run)        DRY_RUN=true; shift ;;
    -h|--help)
      echo "Usage: $0 --domain <FQDN|_> --api-upstream <URL> [--email <certbot-email>] [--certificate <letsencrypt.tar.gz>] [--repo <url>] [--branch <ref>] [--skip-certbot] [--no-firewall | --firewall] [--dry-run]"
      echo "  --domain _      Answer on any host name or address, over HTTP (no certificate)"
      echo "  --no-firewall   Leave ufw alone (the host runs other services)"
      echo "  --firewall      Configure ufw even where deploy-api.sh remembered --no-firewall"
      exit 0
      ;;
    *) fail "Unknown option: $1" ;;
  esac
done

# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------
[[ -n "$DOMAIN" ]]       || fail "Required: --domain <FQDN>"
[[ -n "$API_UPSTREAM" ]] || fail "Required: --api-upstream <URL>  (e.g. http://127.0.0.1:8000)"
# Both go into nginx's config as they're typed (deploy-site.sh checks its names the same way): a domain
# name (or _), and the API's address, http(s):// and a host (a name, an IPv4 or a bracketed IPv6 address)
# with its port, nothing else.
DOMAIN_RE='^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$'
UPSTREAM_RE='^https?://([A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(:[0-9]{1,5})?/?$'
[[ "$DOMAIN" == "_" || "$DOMAIN" =~ $DOMAIN_RE ]] || fail "Not a domain name: ${DOMAIN}"
[[ "$API_UPSTREAM" =~ $UPSTREAM_RE ]] \
  || fail "Not the API's address: ${API_UPSTREAM} (give http://HOST:PORT, e.g. http://127.0.0.1:8000)"

if [[ "$DOMAIN" == *"example.com"* ]]; then
  fail "Replace example.com with your actual domain"
fi
if [[ -n "$CERTIFICATE_ARCHIVE" ]] && [[ ! -f "$CERTIFICATE_ARCHIVE" ]]; then
  fail "Certificate archive not found: $CERTIFICATE_ARCHIVE"
fi
# Any host name: there's no name to get a certificate for.
ANY_HOST=false
if [[ "$DOMAIN" == "_" ]]; then
  ANY_HOST=true
  SKIP_CERTBOT=true
  [[ -z "$CERTIFICATE_ARCHIVE" ]] || fail "--certificate needs a real --domain"
fi

# On the API's machine, follow what deploy-api.sh remembered: both deploy
# /opt/lumiverb, so another branch here would switch the API's code too.
API_CONF_DIR="${LUMIVERB_CONF_DIR:-/etc/lumiverb}"
API_ENV="${API_CONF_DIR}/env"
# It's root's (600, in a 750 folder): read as anyone else it looks absent.
if [[ -e "$API_ENV" && ! -r "$API_ENV" ]] || [[ -d "$API_CONF_DIR" && ! -x "$API_CONF_DIR" ]]; then
  fail "Run with sudo to see the settings remembered in ${API_ENV}"
fi
_api_val() { grep "^${1}=" "$API_ENV" 2>/dev/null | head -1 | cut -d= -f2- || true; }
BRANCH="${BRANCH:-$(_api_val BRANCH)}"; BRANCH="${BRANCH:-main}"
NO_FIREWALL="${NO_FIREWALL:-$(_api_val NO_FIREWALL)}"
[[ "$NO_FIREWALL" == "true" ]] || NO_FIREWALL=false

if [[ "$DRY_RUN" == "true" ]]; then
  echo "DOMAIN=${DOMAIN}"
  echo "API_UPSTREAM=${API_UPSTREAM}"
  echo "BRANCH=${BRANCH}"
  echo "NO_FIREWALL=${NO_FIREWALL}"
  exit 0
fi

[[ "$(id -u)" -eq 0 ]] || fail "This script must be run as root (try: sudo bash ...)"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
APP_DIR="/opt/lumiverb"
NODE_MAJOR=20

# Use lumiverb user if it exists (same-machine with deploy-api), else create it
SVC_USER="lumiverb"

# ---------------------------------------------------------------------------
# Whether the domain points here: Let's Encrypt checks it on port 80 at the
# address DNS gives it, so it's checked before asking, saying what record to
# add and at what address rather than certbot's own error.
# ---------------------------------------------------------------------------
# A v4 address that isn't the internet's (a router's network, CGNAT and Tailscale, link-local, loopback).
private_v4() {
  [[ "$1" =~ ^(10|127)\. || "$1" =~ ^192\.168\. || "$1" =~ ^169\.254\. \
    || "$1" =~ ^172\.(1[6-9]|2[0-9]|3[01])\. || "$1" =~ ^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\. ]]
}

# Says what's wrong and returns 1 when the domain doesn't resolve, or resolves somewhere this machine's public
# address isn't. Behind a router (no public address of that family here) it can't tell, and lets it through.
domain_points_here() {
  local domain="$1" addr resolved mine public_v4="" public_v6="" elsewhere=""
  resolved="$(getent ahosts "$domain" 2>/dev/null | awk '{ print $1 }' | sort -u || true)"
  mine="$(ip -o addr show scope global 2>/dev/null | awk '{ sub(/\/.*/, "", $4); print $4 }' || true)"
  for addr in $mine; do
    if [[ "$addr" == *:* ]]; then
      public_v6="${public_v6:-$addr}"
    elif ! private_v4 "$addr"; then
      public_v4="${public_v4:-$addr}"
    fi
  done
  if [[ -z "$resolved" ]]; then
    warn "${domain} doesn't resolve yet: add an A record for it pointing at ${public_v4:-the address the internet reaches this machine at}"
    return 1
  fi
  for addr in $resolved; do
    [[ "$addr" == 127.* || "$addr" == "::1" ]] && return 0  # /etc/hosts names this machine: DNS isn't asked
    grep -qxF "$addr" <<<"$mine" && continue
    if [[ "$addr" == *:* && -n "$public_v6" ]]; then
      elsewhere+=" Its AAAA record points at ${addr}: change it to ${public_v6}, or remove it."
    elif [[ "$addr" != *:* && -n "$public_v4" ]]; then
      elsewhere+=" Its A record points at ${addr}: change it to ${public_v4}."
    fi
  done
  if [[ -n "$elsewhere" ]]; then
    warn "${domain} doesn't point at this machine.${elsewhere}"
    return 1
  fi
}

# Said first too, so a record added now has the install's few minutes to be seen.
if [[ "$SKIP_CERTBOT" == "false" && -z "$CERTIFICATE_ARCHIVE" ]] && ! domain_points_here "$DOMAIN"; then
  warn "It's checked again before the certificate is asked for"
fi

# ---------------------------------------------------------------------------
# 1. System packages (web-only: nginx, Node.js, git)
# ---------------------------------------------------------------------------
step "Installing system packages"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

apt-get install -y -qq curl ca-certificates gnupg lsb-release

# Node.js repo
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash -
fi

apt-get install -y -qq nginx git nodejs
[[ "$SKIP_CERTBOT" == "true" && -z "$CERTIFICATE_ARCHIVE" ]] || apt-get install -y -qq certbot python3-certbot-nginx
[[ "$NO_FIREWALL" == "true" ]] || apt-get install -y -qq ufw

ok "System packages installed (nginx, Node.js ${NODE_MAJOR}, certbot)"

# ---------------------------------------------------------------------------
# 2. Service user (for file ownership)
# ---------------------------------------------------------------------------
step "Ensuring service user"

id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --shell /usr/sbin/nologin --home /var/lib/lumiverb "$SVC_USER"
ok "User $SVC_USER"

# ---------------------------------------------------------------------------
# 3. Clone / update application
# ---------------------------------------------------------------------------
step "Deploying application to ${APP_DIR}"

# What root runs as the service user reads none of that user's own settings: git's (~/.gitconfig, which can name
# hooks and an fsmonitor to run) and npm's (~/.npmrc). They're the service user's to write, not root's to follow.
# Its home and caches stay as they were.
as_svc() { sudo -u "$SVC_USER" env GIT_CONFIG_GLOBAL=/dev/null npm_config_userconfig=/dev/null "$@"; }

if [[ -d "${APP_DIR}/.git" ]]; then
  # The repo is the service user's, and git runs in it only as that user. Earlier versions marked it safe for root
  # (safe.directory), which let its own settings (.git/config) steer a git root ran there: unmarked.
  git config --system --fixed-value --unset-all safe.directory "$APP_DIR" 2>/dev/null || true
  git config --global --fixed-value --unset-all safe.directory "$APP_DIR" 2>/dev/null || true
  cd "$APP_DIR"
  as_svc git fetch --all --prune
  as_svc git checkout "$BRANCH"
  as_svc git pull origin "$BRANCH"
  ok "Updated existing checkout"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  chown -R "$SVC_USER":"$SVC_USER" "$APP_DIR"
  ok "Cloned ${REPO_URL} @ ${BRANCH}"
fi

cd "$APP_DIR"

# ---------------------------------------------------------------------------
# 4. Build web UI
# ---------------------------------------------------------------------------
step "Building web UI"

cd "$APP_DIR/src/ui/web"
as_svc npm ci --no-audit --no-fund
as_svc npm run build
ok "Web UI built"
cd "$APP_DIR"

# ---------------------------------------------------------------------------
# 5. nginx configuration
# ---------------------------------------------------------------------------
step "Configuring nginx"

# Strip trailing slash from upstream URL
API_UPSTREAM="${API_UPSTREAM%/}"

# The built app loads only its own files and calls only its own /v1/: no
# inline script or style, pictures and videos shown from blob: URLs, video
# streamed from /v1/stream/. What this site gains later reaches an install
# through update-web.sh (src/server/lifecycle/site_update.py, its lines).
CSP="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src 'self' blob:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"

if [[ "$ANY_HOST" == "true" ]]; then
  LISTEN="listen 80 default_server;"
else
  LISTEN="listen 80;"
fi

# Its access log's lines name the site (lumiverb_host, for lumiverb-server site-visits): the format first.
python3 -I "${APP_DIR}/src/server/lifecycle/site_update.py" --log-format "${LUMIVERB_NGINX_CONF_D:-/etc/nginx/conf.d}/lumiverb-log.conf"

cat > /etc/nginx/sites-available/lumiverb <<NGINX
server {
    ${LISTEN}
    server_name ${DOMAIN};
    access_log /var/log/nginx/access.log lumiverb_host;
    access_log /var/log/lumiverb/sites.log lumiverb_host;

    root ${APP_DIR}/src/ui/web/dist;
    index index.html;

    add_header X-Content-Type-Options nosniff always;
    add_header Content-Security-Policy "${CSP}" always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;
    add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;

    # Text goes compressed (the app's code and styles, the API's answers), and
    # nginx doesn't say its version.
    server_tokens off;
    gzip on;
    gzip_types text/css application/javascript text/javascript application/json image/svg+xml;
    gzip_min_length 1024;
    gzip_vary on;

    # Playback: a multi-GB proxy streams straight through instead of being
    # spooled to disk, and the link (a bearer token good for hours) stays
    # out of the access log. Range requests pass through as they are.
    location /v1/stream/ {
        proxy_pass ${API_UPSTREAM};
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

    location /v1/ {
        proxy_pass ${API_UPSTREAM};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        client_max_body_size 100m;
        proxy_connect_timeout 60s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
    }

    # Built files carry a content hash in their name: cache them for good.
    # A location's add_header drops the server's, so they are repeated.
    location /assets/ {
        add_header Cache-Control "public, max-age=31536000, immutable";
        add_header X-Content-Type-Options nosniff always;
        add_header Content-Security-Policy "${CSP}" always;
        add_header X-Frame-Options DENY always;
        add_header Referrer-Policy strict-origin-when-cross-origin always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;
    }

    # The page names the build's files, so the browser checks it every time
    # (the SPA fallback below ends here too).
    location = /index.html {
        add_header Cache-Control "no-cache";
        add_header X-Content-Type-Options nosniff always;
        add_header Content-Security-Policy "${CSP}" always;
        add_header X-Frame-Options DENY always;
        add_header Referrer-Policy strict-origin-when-cross-origin always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;
    }

    # The app's pages: any path that isn't one of its files. A hidden file
    # (/.env, /.git/) or a file name it doesn't have is a 404, never the page.
    location / {
        location ~ /\\. {
            return 404;
        }
        location ~ \\.[A-Za-z0-9]+\$ {
            try_files \$uri =404;
        }
        try_files \$uri \$uri/ /index.html;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/lumiverb /etc/nginx/sites-enabled/lumiverb
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
ok "nginx configured: ${DOMAIN} -> API at ${API_UPSTREAM}"

# ---------------------------------------------------------------------------
# 6. Firewall (before the certificate: Let's Encrypt checks the domain on port 80)
# ---------------------------------------------------------------------------
if [[ "$NO_FIREWALL" == "true" ]]; then
  step "Firewall"
  ok "Left alone (--no-firewall)"
else
  step "Configuring firewall (ufw)"

  ufw default deny incoming
  ufw default allow outgoing
  ufw allow 22/tcp   # SSH
  ufw allow 80/tcp   # HTTP (certbot + redirect)
  ufw allow 443/tcp  # HTTPS
  ufw --force enable
  ok "Firewall active — inbound limited to SSH, HTTP, HTTPS"
fi

# ---------------------------------------------------------------------------
# 7. TLS certificate
# ---------------------------------------------------------------------------
TLS_ACTIVE=false

# HSTS on HTTPS server blocks only: beside each nosniff header in a block
# that listens on 443 (certbot's port-80 redirect block has none).
add_hsts() {
  local site="$1"
  grep -q "Strict-Transport-Security" "$site" && return 0
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
  ' "$site" > "${site}.new"
  mv "${site}.new" "$site"
}

# A name none of this machine's sites serves (its bare address, someone else's domain pointing here) gets
# nothing: the connection closed, and over HTTPS before any certificate is shown (ssl_reject_handshake, nginx
# 1.19.4; an older one turns them away over HTTP alone). On IPv6 only where a site here listens there: a
# listener of its own would answer that port for every name. For a web app with a name of its own (--domain _
# answers every name). tests/test_site_update.py holds deploy-web.sh's and update-web.sh's to the same text.
version_at_least() { [[ "$(printf '%s\n' "$2" "$1" | sort -V | head -1)" == "$2" ]]; }
default_site() {
  local enabled="$1" version="$2" listens port ssl
  listens="$(grep -rhsE '^[[:space:]]*listen[[:space:]]' --exclude=lumiverb-default "$enabled" || true)"
  echo "# Lumiverb's (deploy-web.sh, update-web.sh): a name no site here serves gets nothing."
  echo "server {"
  for port in 80 443; do
    ssl=""
    if [[ "$port" == 443 ]]; then
      version_at_least "$version" 1.19.4 || continue
      ssl=" ssl"
    fi
    echo "    listen ${port}${ssl} default_server;"
    if grep -qE "^[[:space:]]*listen[[:space:]]+\[::\]:${port}[[:space:];]" <<<"$listens"; then
      echo "    listen [::]:${port}${ssl} default_server;"
    fi
  done
  echo "    server_name _;"
  echo "    server_tokens off;"
  if version_at_least "$version" 1.19.4; then
    echo "    ssl_reject_handshake on;"
  fi
  echo "    return 444;"
  echo "}"
}
install_default_site() {
  local available="${LUMIVERB_NGINX_SITES:-/etc/nginx/sites-available}" enabled="${LUMIVERB_NGINX_ENABLED:-/etc/nginx/sites-enabled}"
  default_site "$enabled" "$(nginx -v 2>&1 | sed -nE 's|.*nginx/([0-9.]+).*|\1|p')" > "${available}/lumiverb-default"
  ln -sf "${available}/lumiverb-default" "${enabled}/lumiverb-default"
  # One nginx refuses (another site here is the default already) would stop every later reload: gone again.
  if ! nginx -t >/dev/null 2>&1; then
    rm -f "${enabled}/lumiverb-default"
    warn "nginx refused a default site for names it doesn't serve (another site is the default?): left as it was"
    return 0
  fi
  ok "A name no site here serves gets nothing"
}

if [[ -n "$CERTIFICATE_ARCHIVE" ]]; then
  step "Restoring TLS certificate from archive"
  tar xzf "$CERTIFICATE_ARCHIVE" -C /
  ok "Restored /etc/letsencrypt from $CERTIFICATE_ARCHIVE"
  certbot install --nginx -d "$DOMAIN" --non-interactive --redirect
  ok "TLS certificate installed into nginx"
  TLS_ACTIVE=true

elif [[ "$SKIP_CERTBOT" == "false" ]]; then
  step "Obtaining TLS certificate"

  certbot_args=(--nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect)
  if [[ -n "$CERTBOT_EMAIL" ]]; then
    certbot_args+=(--email "$CERTBOT_EMAIL")
  else
    certbot_args+=(--register-unsafely-without-email)
  fi

  if ! domain_points_here "$DOMAIN"; then
    warn "Not asked for yet: the site serves HTTP only until ${DOMAIN} points here; then run this again"
  elif certbot "${certbot_args[@]}"; then
    ok "TLS certificate obtained"
    TLS_ACTIVE=true
  else
    warn "Certbot failed — site will serve HTTP only until the domain points here and port 80 is reachable; then run this again"
  fi
else
  warn "Skipping certbot (--skip-certbot)"
fi

if [[ "$TLS_ACTIVE" == "true" ]]; then
  add_hsts /etc/nginx/sites-available/lumiverb
  ok "HSTS on"
  # HTTP/2 on its HTTPS, in the words this nginx takes (update-web.sh gives an existing site the same).
  python3 -I "${APP_DIR}/src/server/lifecycle/site_update.py" /etc/nginx/sites-available/lumiverb
  nginx -t
  systemctl reload nginx
fi

if [[ "$ANY_HOST" == "false" ]]; then
  step "Turning away names this machine doesn't serve"
  install_default_site
  systemctl reload nginx
fi

# ---------------------------------------------------------------------------
# 8. Verify
# ---------------------------------------------------------------------------
step "Verifying"

if [[ -d "${APP_DIR}/src/ui/web/dist" ]]; then
  ok "SPA build exists"
else
  warn "SPA dist directory not found"
fi

if curl -sf "${API_UPSTREAM}/health" >/dev/null 2>&1; then
  ok "API upstream reachable"
else
  warn "API upstream not reachable at ${API_UPSTREAM}/health — verify connectivity"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
if [[ "$TLS_ACTIVE" == "true" ]]; then
  SITE_URL="https://${DOMAIN}"
elif [[ "$ANY_HOST" == "true" ]]; then
  SITE_URL="http://<this machine's address> (port 80, any host name)"
else
  SITE_URL="http://${DOMAIN}"
fi

echo -e "${GREEN}${BOLD}Lumiverb web UI deployed to ${SITE_URL}${NC}"
echo ""
echo "API upstream: ${API_UPSTREAM}"
echo ""
echo "To update (sub-10 seconds, no API restart):"
echo "  bash /opt/lumiverb/scripts/update-web.sh"
echo ""
if [[ "$TLS_ACTIVE" == "true" ]] && [[ -z "$CERTIFICATE_ARCHIVE" ]]; then
  echo "Back up the TLS certificate for future redeploys:"
  echo "  tar czf /tmp/letsencrypt.tar.gz -C / etc/letsencrypt"
  echo ""
fi
