#!/usr/bin/env bash
# Bootstrap the Lumiverb web UI on a fresh Ubuntu 22.04+ machine.
#
# Installs nginx + Node.js, clones the repo, builds the SPA, configures
# nginx to proxy /v1/* to the API server. Does NOT install Python,
# PostgreSQL, Quickwit, or any API server components.
#
# Usage:
#   bash scripts/deploy-web.sh --domain app.example.com --api-upstream http://10.0.0.5:8000
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

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
DOMAIN=""
API_UPSTREAM=""
REPO_URL="https://github.com/bubbafat/lumiverb.git"
BRANCH="main"
CERTBOT_EMAIL=""
SKIP_CERTBOT=false
CERTIFICATE_ARCHIVE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain)         DOMAIN="${2:?Missing value for --domain}"; shift 2 ;;
    --api-upstream)   API_UPSTREAM="${2:?Missing value for --api-upstream}"; shift 2 ;;
    --repo)           REPO_URL="${2:?Missing value for --repo}"; shift 2 ;;
    --branch)         BRANCH="${2:?Missing value for --branch}"; shift 2 ;;
    --email)          CERTBOT_EMAIL="${2:?Missing value for --email}"; shift 2 ;;
    --certificate)    CERTIFICATE_ARCHIVE="${2:?Missing value for --certificate}"; shift 2 ;;
    --skip-certbot)   SKIP_CERTBOT=true; shift ;;
    -h|--help)
      echo "Usage: $0 --domain <FQDN> --api-upstream <URL> [--email <certbot-email>] [--certificate <letsencrypt.tar.gz>] [--repo <url>] [--branch <ref>] [--skip-certbot]"
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

if [[ "$DOMAIN" == *"example.com"* ]]; then
  fail "Replace example.com with your actual domain"
fi
if [[ -n "$CERTIFICATE_ARCHIVE" ]] && [[ ! -f "$CERTIFICATE_ARCHIVE" ]]; then
  fail "Certificate archive not found: $CERTIFICATE_ARCHIVE"
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

apt-get install -y -qq nginx certbot python3-certbot-nginx git nodejs ufw

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

if [[ -d "${APP_DIR}/.git" ]]; then
  # Repo is owned by $SVC_USER; tell git it's safe for root.
  git config --system --replace-all safe.directory "$APP_DIR" "$APP_DIR" 2>/dev/null \
    || git config --global --add safe.directory "$APP_DIR"
  cd "$APP_DIR"
  sudo -u "$SVC_USER" git fetch --all --prune
  sudo -u "$SVC_USER" git checkout "$BRANCH"
  sudo -u "$SVC_USER" git pull origin "$BRANCH"
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
sudo -u "$SVC_USER" npm ci --no-audit --no-fund
sudo -u "$SVC_USER" npm run build
ok "Web UI built"
cd "$APP_DIR"

# ---------------------------------------------------------------------------
# 5. nginx configuration
# ---------------------------------------------------------------------------
step "Configuring nginx"

# Strip trailing slash from upstream URL
API_UPSTREAM="${API_UPSTREAM%/}"

cat > /etc/nginx/sites-available/lumiverb <<NGINX
server {
    listen 80;
    server_name ${DOMAIN};

    root ${APP_DIR}/src/ui/web/dist;
    index index.html;

    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer-when-downgrade always;

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

    location / {
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
# 6. TLS certificate
# ---------------------------------------------------------------------------
TLS_ACTIVE=false

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

  if certbot "${certbot_args[@]}"; then
    ok "TLS certificate obtained"
    TLS_ACTIVE=true
  else
    warn "Certbot failed — site will serve HTTP only until DNS is pointed and certbot re-run"
  fi
else
  warn "Skipping certbot (--skip-certbot)"
fi

# ---------------------------------------------------------------------------
# 7. Firewall
# ---------------------------------------------------------------------------
step "Configuring firewall (ufw)"

ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp   # SSH
ufw allow 80/tcp   # HTTP (certbot + redirect)
ufw allow 443/tcp  # HTTPS
ufw --force enable
ok "Firewall active — inbound limited to SSH, HTTP, HTTPS"

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
