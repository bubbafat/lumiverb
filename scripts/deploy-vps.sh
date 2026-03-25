#!/usr/bin/env bash
# Bootstrap Lumiverb on a fresh Ubuntu 22.04+ VPS (DigitalOcean, Hetzner, etc.)
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/bubbafat/lumiverb/main/scripts/deploy-vps.sh | bash -s -- --domain app.example.com
#
# Or from a local checkout:
#   bash scripts/deploy-vps.sh --domain app.example.com
#
# Idempotent: safe to run again to update an existing install.
#
# After completion, create the first admin user:
#   cd /opt/lumiverb && sudo -u lumiverb .venv/bin/lumiverb create-user --email you@example.com --role admin
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
REPO_URL="https://github.com/bubbafat/lumiverb.git"
BRANCH="main"
CERTBOT_EMAIL=""
SKIP_CERTBOT=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain)       DOMAIN="${2:?Missing value for --domain}"; shift 2 ;;
    --repo)         REPO_URL="${2:?Missing value for --repo}"; shift 2 ;;
    --branch)       BRANCH="${2:?Missing value for --branch}"; shift 2 ;;
    --email)        CERTBOT_EMAIL="${2:?Missing value for --email}"; shift 2 ;;
    --skip-certbot) SKIP_CERTBOT=true; shift ;;
    -h|--help)
      echo "Usage: $0 --domain <FQDN> [--email <certbot-email>] [--repo <url>] [--branch <ref>] [--skip-certbot]"
      exit 0
      ;;
    *) fail "Unknown option: $1" ;;
  esac
done

[[ -n "$DOMAIN" ]] || fail "Required: --domain <FQDN>  (e.g. --domain app.example.com)"

# Must run as root
[[ "$(id -u)" -eq 0 ]] || fail "This script must be run as root (try: sudo bash ...)"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
APP_DIR="/opt/lumiverb"
CONF_DIR="/etc/lumiverb"
DATA_DIR="/var/lib/lumiverb"
BACKUP_DIR="/var/backups/lumiverb"
ENV_FILE="${CONF_DIR}/env"
SVC_USER="lumiverb"
PG_USER="app"
PG_DB="control_plane"
NODE_MAJOR=20
UV_VERSION="0.7.12"
UV_BIN="/usr/local/bin/uv"
QUICKWIT_VERSION="0.8.2"

# Map host architecture to release artifact names
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)  RUST_TARGET="x86_64-unknown-linux-gnu" ;;
  aarch64) RUST_TARGET="aarch64-unknown-linux-gnu" ;;
  *) fail "Unsupported architecture: $ARCH (only x86_64 and aarch64 are supported)" ;;
esac

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
step "Installing system packages"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

# Bootstrap minimal deps needed by the rest of the script.
apt-get install -y -qq curl ca-certificates gnupg lsb-release openssl

# PostgreSQL 16 repo (if not already present)
if ! apt-cache show postgresql-16 >/dev/null 2>&1; then
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /usr/share/keyrings/postgresql.gpg
  echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update -qq
fi

# Node.js repo — NodeSource's setup script is the standard method for adding their
# apt repo. It registers an HTTPS source and GPG key; packages are then verified
# by apt on install. There is no checksum-verifiable alternative.
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash -
fi

apt-get install -y -qq \
  postgresql-16 postgresql-16-pgvector \
  nginx certbot python3-certbot-nginx \
  git build-essential \
  nodejs ufw

ok "System packages installed"

# ---------------------------------------------------------------------------
# 2. uv (Python package manager)
# ---------------------------------------------------------------------------
step "Installing uv"

if [[ ! -x "$UV_BIN" ]]; then
  UV_ARCHIVE="uv-${RUST_TARGET}.tar.gz"
  curl -LsSf "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${UV_ARCHIVE}" \
    -o /tmp/uv.tar.gz
  curl -LsSf "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${UV_ARCHIVE}.sha256" \
    -o /tmp/uv.sha256
  EXPECTED_HASH="$(awk '{print $1}' /tmp/uv.sha256)"
  ACTUAL_HASH="$(sha256sum /tmp/uv.tar.gz | awk '{print $1}')"
  [[ "$EXPECTED_HASH" == "$ACTUAL_HASH" ]] || fail "uv checksum verification failed"
  tar -xzf /tmp/uv.tar.gz -C /tmp
  install -m 0755 "/tmp/uv-${RUST_TARGET}/uv" "$UV_BIN"
  install -m 0755 "/tmp/uv-${RUST_TARGET}/uvx" /usr/local/bin/uvx
  rm -rf /tmp/uv.tar.gz /tmp/uv.sha256 "/tmp/uv-${RUST_TARGET}"
fi
ok "uv $($UV_BIN --version)"

# ---------------------------------------------------------------------------
# 3. Service user and directories
# ---------------------------------------------------------------------------
step "Creating service user and directories"

id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --shell /usr/sbin/nologin --home "$DATA_DIR" "$SVC_USER"

mkdir -p "$CONF_DIR" "$DATA_DIR"/{proxies,thumbnails,quickwit} "$BACKUP_DIR"
chown -R root:root "$CONF_DIR"
chmod 700 "$CONF_DIR"
chown -R "$SVC_USER":"$SVC_USER" "$DATA_DIR"

ok "User $SVC_USER, dirs ready"

# ---------------------------------------------------------------------------
# 4. PostgreSQL bootstrap
# ---------------------------------------------------------------------------
step "Configuring PostgreSQL"

# Generate a stable DB password (create once, reuse on re-runs)
if [[ -f "${CONF_DIR}/.pg_password" ]]; then
  PG_PASS="$(cat "${CONF_DIR}/.pg_password")"
else
  PG_PASS="$(openssl rand -hex 24)"
  echo -n "$PG_PASS" > "${CONF_DIR}/.pg_password"
  chmod 600 "${CONF_DIR}/.pg_password"
fi

# Enforce listen_addresses = localhost (handles commented, uncommented, or missing)
PG_CONF="/etc/postgresql/16/main/postgresql.conf"
PG_NEEDS_RESTART=false
if grep -qE "^\s*listen_addresses\s*=" "$PG_CONF" 2>/dev/null; then
  current=$(grep -oP "^\s*listen_addresses\s*=\s*'\K[^']+" "$PG_CONF" || true)
  if [[ "$current" != "127.0.0.1" ]]; then
    sed -i "s/^\s*listen_addresses\s*=.*/listen_addresses = '127.0.0.1'/" "$PG_CONF"
    PG_NEEDS_RESTART=true
  fi
elif grep -qE "^#\s*listen_addresses" "$PG_CONF" 2>/dev/null; then
  sed -i "s/^#\s*listen_addresses.*/listen_addresses = '127.0.0.1'/" "$PG_CONF"
  PG_NEEDS_RESTART=true
else
  echo "listen_addresses = '127.0.0.1'" >> "$PG_CONF"
  PG_NEEDS_RESTART=true
fi
if [[ "$PG_NEEDS_RESTART" == "true" ]]; then
  systemctl restart postgresql
fi

# Create user + DB if they don't exist
su - postgres -c "psql -tc \"SELECT 1 FROM pg_roles WHERE rolname='${PG_USER}'\"" | grep -q 1 \
  || su - postgres -c "psql -c \"CREATE USER ${PG_USER} WITH PASSWORD '${PG_PASS}' CREATEDB\""

# Update password and ensure CREATEDB on re-runs (idempotent)
su - postgres -c "psql -c \"ALTER USER ${PG_USER} WITH PASSWORD '${PG_PASS}' CREATEDB\""

su - postgres -c "psql -tc \"SELECT 1 FROM pg_database WHERE datname='${PG_DB}'\"" | grep -q 1 \
  || su - postgres -c "psql -c \"CREATE DATABASE ${PG_DB} OWNER ${PG_USER}\""

# Install pgvector in template1 so all new tenant databases inherit it,
# and in control_plane for the control plane schema.
su - postgres -c "psql -d template1 -c 'CREATE EXTENSION IF NOT EXISTS vector'"
su - postgres -c "psql -d ${PG_DB} -c 'CREATE EXTENSION IF NOT EXISTS vector'"

ok "PostgreSQL: user=${PG_USER}, db=${PG_DB}, pgvector enabled (template1 + ${PG_DB})"

# ---------------------------------------------------------------------------
# 5. Generate secrets and write env file
# ---------------------------------------------------------------------------
step "Writing ${ENV_FILE}"

# Preserve existing secrets on re-run
_existing_val() {
  grep "^${1}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true
}

ADMIN_KEY="$(_existing_val ADMIN_KEY)"
API_SECRET_KEY="$(_existing_val API_SECRET_KEY)"
JWT_SECRET="$(_existing_val JWT_SECRET)"

[[ -n "$ADMIN_KEY" ]]     || ADMIN_KEY="$(openssl rand -hex 32)"
[[ -n "$API_SECRET_KEY" ]] || API_SECRET_KEY="$(openssl rand -hex 32)"
[[ -n "$JWT_SECRET" ]]     || JWT_SECRET="$(openssl rand -hex 32)"

DB_URL="postgresql+psycopg2://${PG_USER}:${PG_PASS}@127.0.0.1:5432"

cat > "$ENV_FILE" <<ENVEOF
# Auto-generated by deploy-vps.sh — $(date -u +%Y-%m-%dT%H:%M:%SZ)

# Database
CONTROL_PLANE_DATABASE_URL=${DB_URL}/${PG_DB}
TENANT_DATABASE_URL_TEMPLATE=${DB_URL}/{tenant_id}

# Auth
ADMIN_KEY=${ADMIN_KEY}
API_SECRET_KEY=${API_SECRET_KEY}
JWT_SECRET=${JWT_SECRET}

# Storage
STORAGE_PROVIDER=local
DATA_DIR=${DATA_DIR}

# Search
QUICKWIT_URL=http://127.0.0.1:7280
QUICKWIT_ENABLED=true

# App
APP_ENV=production
APP_HOST=https://${DOMAIN}
LOG_LEVEL=INFO

# Password reset SMTP (uncomment and fill in to enable forgot-password)
# SMTP_HOST=smtp.example.com
# SMTP_PORT=587
# SMTP_USER=apikey
# SMTP_PASSWORD=
# SMTP_FROM=noreply@${DOMAIN}
ENVEOF

chmod 600 "$ENV_FILE"
ok "Secrets generated, env written"

# ---------------------------------------------------------------------------
# 6. Quickwit
# ---------------------------------------------------------------------------
step "Installing Quickwit"

if ! command -v quickwit >/dev/null 2>&1; then
  # Note: Quickwit does not publish checksum files. Pinned version + HTTPS provide
  # baseline integrity. If you need stronger guarantees, download manually and verify
  # the GitHub release digest before deploying.
  QW_ARCHIVE="quickwit-v${QUICKWIT_VERSION}-${RUST_TARGET}.tar.gz"
  QW_TMP="$(mktemp -d)"
  curl -LsSf "https://github.com/quickwit-oss/quickwit/releases/download/v${QUICKWIT_VERSION}/${QW_ARCHIVE}" \
    -o "${QW_TMP}/quickwit.tar.gz"
  tar -xzf "${QW_TMP}/quickwit.tar.gz" -C "$QW_TMP"
  QW_BIN="$(find "$QW_TMP" -name quickwit -type f -executable | head -1)"
  [[ -n "$QW_BIN" ]] || fail "Could not find quickwit binary in downloaded archive"
  install -m 0755 "$QW_BIN" /usr/local/bin/quickwit
  rm -rf "$QW_TMP"
fi
ok "quickwit $(quickwit --version 2>&1 | head -1)"

cat > /etc/systemd/system/lumiverb-quickwit.service <<'UNIT'
[Unit]
Description=Lumiverb Quickwit
After=network.target

[Service]
Type=simple
User=lumiverb
Group=lumiverb
ExecStart=/usr/local/bin/quickwit run --service metastore --service indexer --service searcher --data-dir /var/lib/lumiverb/quickwit --listen-address 127.0.0.1 --rest-listen-port 7280
Restart=on-failure
RestartSec=5s
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/lumiverb/quickwit

[Install]
WantedBy=multi-user.target
UNIT

# ---------------------------------------------------------------------------
# 7. Clone / update application
# ---------------------------------------------------------------------------
step "Deploying application to ${APP_DIR}"

if [[ -d "${APP_DIR}/.git" ]]; then
  cd "$APP_DIR"
  git fetch --all --prune
  git checkout "$BRANCH"
  git pull origin "$BRANCH"
  ok "Updated existing checkout"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  ok "Cloned ${REPO_URL} @ ${BRANCH}"
fi

cd "$APP_DIR"
chown -R "$SVC_USER":"$SVC_USER" "$APP_DIR"

# ---------------------------------------------------------------------------
# 8. Python dependencies
# ---------------------------------------------------------------------------
step "Installing Python dependencies"

sudo -u "$SVC_USER" "$UV_BIN" sync --all-extras
ok "Python venv ready"

# ---------------------------------------------------------------------------
# 9. Run migrations
# ---------------------------------------------------------------------------
step "Running database migrations"

export ALEMBIC_CONTROL_URL="${DB_URL}/${PG_DB}"
sudo -u "$SVC_USER" --preserve-env=ALEMBIC_CONTROL_URL \
  "$APP_DIR/.venv/bin/python" -m alembic -c alembic-control.ini upgrade head
ok "Control plane migrations applied"

# ---------------------------------------------------------------------------
# 10. Build web UI
# ---------------------------------------------------------------------------
step "Building web UI"

cd "$APP_DIR/src/ui/web"
sudo -u "$SVC_USER" npm ci --no-audit --no-fund
sudo -u "$SVC_USER" npm run build
ok "Web UI built"
cd "$APP_DIR"

# ---------------------------------------------------------------------------
# 11. systemd units
# ---------------------------------------------------------------------------
step "Installing systemd units"

cat > /etc/systemd/system/lumiverb-api.service <<UNIT
[Unit]
Description=Lumiverb API Server
After=network.target postgresql.service lumiverb-quickwit.service
Wants=lumiverb-quickwit.service

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
ExecStart=${APP_DIR}/.venv/bin/uvicorn src.api.main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=on-failure
RestartSec=5s
LimitNOFILE=65535
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${DATA_DIR}

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/lumiverb-worker.service <<UNIT
[Unit]
Description=Lumiverb Background Worker
After=network.target postgresql.service lumiverb-api.service
Wants=lumiverb-api.service

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
ExecStart=${APP_DIR}/.venv/bin/python -m src.workers.main
Restart=on-failure
RestartSec=10s
LimitNOFILE=65535
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${DATA_DIR}

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
ok "systemd units installed"

# ---------------------------------------------------------------------------
# 12. nginx
# ---------------------------------------------------------------------------
step "Configuring nginx"

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
        proxy_pass http://127.0.0.1:8000;
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
ok "nginx configured for ${DOMAIN}"

# ---------------------------------------------------------------------------
# 13. TLS certificate
# ---------------------------------------------------------------------------
if [[ "$SKIP_CERTBOT" == "false" ]]; then
  step "Obtaining TLS certificate"

  certbot_args=(--nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect)
  if [[ -n "$CERTBOT_EMAIL" ]]; then
    certbot_args+=(--email "$CERTBOT_EMAIL")
  else
    certbot_args+=(--register-unsafely-without-email)
  fi

  if certbot "${certbot_args[@]}"; then
    ok "TLS certificate obtained"
  else
    warn "Certbot failed — site will serve HTTP only until DNS is pointed and certbot re-run"
  fi
else
  warn "Skipping certbot (--skip-certbot)"
fi

# ---------------------------------------------------------------------------
# 14. Firewall
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
# 15. Start services
# ---------------------------------------------------------------------------
step "Starting services"

systemctl enable --now lumiverb-quickwit lumiverb-api lumiverb-worker

# Brief wait for API to come up
for i in {1..10}; do
  if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
  ok "API server healthy"
else
  warn "API server not responding yet — check: journalctl -u lumiverb-api -n 50"
fi

systemctl status --no-pager lumiverb-quickwit lumiverb-api lumiverb-worker || true

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}Lumiverb deployed to https://${DOMAIN}${NC}"
echo ""
echo "Next steps:"
echo ""
echo "  1. Provision a tenant:"
echo ""
echo "     ADMIN_KEY=\$(sudo grep '^ADMIN_KEY=' ${ENV_FILE} | cut -d= -f2-)"
echo "     curl -s -X POST http://127.0.0.1:8000/v1/admin/tenants \\"
echo "       -H \"Authorization: Bearer \$ADMIN_KEY\" \\"
echo "       -H \"Content-Type: application/json\" \\"
echo "       -d '{\"name\": \"My Org\", \"email\": \"you@example.com\"}'"
echo ""
echo "  2. Configure the CLI with the api_key from the response:"
echo ""
echo "     sudo -u lumiverb mkdir -p /var/lib/lumiverb/.lumiverb"
echo "     echo '{\"api_url\": \"http://127.0.0.1:8000\", \"api_key\": \"<api_key>\"}' \\"
echo "       | sudo -u lumiverb tee /var/lib/lumiverb/.lumiverb/config.json > /dev/null"
echo ""
echo "  3. Create the first admin user:"
echo ""
echo "     sudo -u lumiverb /opt/lumiverb/.venv/bin/lumiverb create-user --email you@example.com --role admin"
echo ""
echo "  4. Open https://${DOMAIN} and log in."
echo ""
echo "  5. (Optional) Enable password reset — edit ${ENV_FILE} and uncomment the SMTP_* lines."
echo ""
echo "Useful commands:"
echo "  journalctl -u lumiverb-api -f          # API logs"
echo "  journalctl -u lumiverb-worker -f       # Worker logs"
echo "  systemctl restart lumiverb-api          # Restart API"
echo ""
echo "Config: ${ENV_FILE} (contains secrets — use 'sudo cat' with care)"
echo ""
