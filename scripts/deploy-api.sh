#!/usr/bin/env bash
# Bootstrap the Lumiverb API server on a fresh Ubuntu 22.04+ machine.
#
# Installs PostgreSQL, Quickwit, Python/uv, runs migrations, creates systemd
# units. Does NOT install nginx, Node.js, or build the web UI — use
# deploy-web.sh for that (same or different machine).
#
# Usage:
#   bash scripts/deploy-api.sh --domain api.example.com
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
REPO_URL="https://github.com/bubbafat/lumiverb.git"
BRANCH="main"
CERTBOT_EMAIL=""
TENANT_NAME="Lumiverb"
DATA_DIR_OVERRIDE=""
VISION_API_URL=""
VISION_API_KEY=""
API_LISTEN_HOST=""
API_ALLOW_FROM=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain)           DOMAIN="${2:?Missing value for --domain}"; shift 2 ;;
    --repo)             REPO_URL="${2:?Missing value for --repo}"; shift 2 ;;
    --branch)           BRANCH="${2:?Missing value for --branch}"; shift 2 ;;
    --email)            CERTBOT_EMAIL="${2:?Missing value for --email}"; shift 2 ;;
    --tenant)           TENANT_NAME="${2:?Missing value for --tenant}"; shift 2 ;;
    --data-dir)         DATA_DIR_OVERRIDE="${2:?Missing value for --data-dir}"; shift 2 ;;
    --vision-api-url)   VISION_API_URL="${2:?Missing value for --vision-api-url}"; shift 2 ;;
    --vision-api-key)   VISION_API_KEY="${2:?Missing value for --vision-api-key}"; shift 2 ;;
    --api-listen-host)  API_LISTEN_HOST="${2:?Missing value for --api-listen-host}"; shift 2 ;;
    --api-allow-from)   API_ALLOW_FROM="${2:?Missing value for --api-allow-from}"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 --domain <FQDN> [--email <email>] [--tenant <name>] [--data-dir <path>] [--api-listen-host <ip>] [--api-allow-from <cidr>] [--vision-api-url <url>] [--vision-api-key <key>] [--repo <url>] [--branch <ref>]"
      exit 0
      ;;
    *) fail "Unknown option: $1" ;;
  esac
done

# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------
[[ -n "$DOMAIN" ]] || fail "Required: --domain <FQDN>  (e.g. --domain api.example.com)"

if [[ "$DOMAIN" == *"example.com"* ]]; then
  fail "Replace example.com with your actual domain"
fi

[[ "$(id -u)" -eq 0 ]] || fail "This script must be run as root (try: sudo bash ...)"

if [[ -n "$DATA_DIR_OVERRIDE" ]]; then
  mkdir -p "$DATA_DIR_OVERRIDE" || fail "Cannot create data directory: $DATA_DIR_OVERRIDE"
fi

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
APP_DIR="/opt/lumiverb"
CONF_DIR="/etc/lumiverb"
DATA_DIR="${DATA_DIR_OVERRIDE:-/var/lib/lumiverb}"
BACKUP_DIR="/var/backups/lumiverb"
ENV_FILE="${CONF_DIR}/env"
SVC_USER="lumiverb"
PG_USER="app"
PG_DB="control_plane"
UV_VERSION="0.7.12"
UV_BIN="/usr/local/bin/uv"
QUICKWIT_VERSION="0.8.2"

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)  RUST_TARGET="x86_64-unknown-linux-gnu" ;;
  aarch64) RUST_TARGET="aarch64-unknown-linux-gnu" ;;
  *) fail "Unsupported architecture: $ARCH (only x86_64 and aarch64 are supported)" ;;
esac

# ---------------------------------------------------------------------------
# 1. System packages (API-only: no Node.js, no nginx)
# ---------------------------------------------------------------------------
step "Installing system packages"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

apt-get install -y -qq curl ca-certificates gnupg lsb-release openssl

# PostgreSQL 16 repo
if ! apt-cache show postgresql-16 >/dev/null 2>&1; then
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /usr/share/keyrings/postgresql.gpg
  echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update -qq
fi

apt-get install -y -qq \
  postgresql-16 postgresql-16-pgvector \
  git build-essential ufw

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

SVC_HOME="/var/lib/lumiverb"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --shell /usr/sbin/nologin --home "$SVC_HOME" "$SVC_USER"

mkdir -p "$SVC_HOME" "$CONF_DIR" "$DATA_DIR"/quickwit "$BACKUP_DIR"
chown "$SVC_USER":"$SVC_USER" "$SVC_HOME"
chown root:"$SVC_USER" "$CONF_DIR"
chmod 750 "$CONF_DIR"
chown -R "$SVC_USER":"$SVC_USER" "$DATA_DIR"

ok "User $SVC_USER, dirs ready"

# ---------------------------------------------------------------------------
# 4. PostgreSQL bootstrap
# ---------------------------------------------------------------------------
step "Configuring PostgreSQL"

if [[ -f "${CONF_DIR}/.pg_password" ]]; then
  PG_PASS="$(cat "${CONF_DIR}/.pg_password")"
else
  PG_PASS="$(openssl rand -hex 24)"
  echo -n "$PG_PASS" > "${CONF_DIR}/.pg_password"
  chmod 600 "${CONF_DIR}/.pg_password"
fi

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

su - postgres -c "psql -tc \"SELECT 1 FROM pg_roles WHERE rolname='${PG_USER}'\"" | grep -q 1 \
  || su - postgres -c "psql -c \"CREATE USER ${PG_USER} WITH PASSWORD '${PG_PASS}' CREATEDB\""

su - postgres -c "psql -c \"ALTER USER ${PG_USER} WITH PASSWORD '${PG_PASS}' CREATEDB\""

su - postgres -c "psql -tc \"SELECT 1 FROM pg_database WHERE datname='${PG_DB}'\"" | grep -q 1 \
  || su - postgres -c "psql -c \"CREATE DATABASE ${PG_DB} OWNER ${PG_USER}\""

su - postgres -c "psql -d template1 -c 'CREATE EXTENSION IF NOT EXISTS vector'"
su - postgres -c "psql -d ${PG_DB} -c 'CREATE EXTENSION IF NOT EXISTS vector'"

ok "PostgreSQL: user=${PG_USER}, db=${PG_DB}, pgvector enabled"

# ---------------------------------------------------------------------------
# 5. Generate secrets and write env file
# ---------------------------------------------------------------------------
step "Writing ${ENV_FILE}"

_existing_val() {
  grep "^${1}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true
}

ADMIN_KEY="$(_existing_val ADMIN_KEY)"
API_SECRET_KEY="$(_existing_val API_SECRET_KEY)"
JWT_SECRET="$(_existing_val JWT_SECRET)"

[[ -n "$ADMIN_KEY" ]]     || ADMIN_KEY="$(openssl rand -hex 32)"
[[ -n "$API_SECRET_KEY" ]] || API_SECRET_KEY="$(openssl rand -hex 32)"
[[ -n "$JWT_SECRET" ]]     || JWT_SECRET="$(openssl rand -hex 32)"

# Resolve API listen host: CLI flag > existing env > default
if [[ -z "$API_LISTEN_HOST" ]]; then
  API_LISTEN_HOST="$(_existing_val API_LISTEN_HOST)"
fi
API_LISTEN_HOST="${API_LISTEN_HOST:-127.0.0.1}"

DB_URL="postgresql+psycopg2://${PG_USER}:${PG_PASS}@127.0.0.1:5432"

cat > "$ENV_FILE" <<ENVEOF
# Auto-generated by deploy-api.sh — $(date -u +%Y-%m-%dT%H:%M:%SZ)

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

# API
API_LISTEN_HOST=${API_LISTEN_HOST}

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
ok "Secrets generated, env written (API_LISTEN_HOST=${API_LISTEN_HOST})"

# ---------------------------------------------------------------------------
# 6. Quickwit
# ---------------------------------------------------------------------------
step "Installing Quickwit"

if ! command -v quickwit >/dev/null 2>&1; then
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

cat > "${CONF_DIR}/quickwit.yaml" <<QWCONF
version: 0.8
node_id: lumiverb
listen_address: 127.0.0.1
rest:
  listen_port: 7280
data_dir: ${DATA_DIR}/quickwit
QWCONF
chmod 644 "${CONF_DIR}/quickwit.yaml"

cat > /etc/systemd/system/lumiverb-quickwit.service <<UNIT
[Unit]
Description=Lumiverb Quickwit
After=network.target

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_USER}
ExecStart=/usr/local/bin/quickwit run --config ${CONF_DIR}/quickwit.yaml
Restart=on-failure
RestartSec=5s
NoNewPrivileges=true

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

sudo -u "$SVC_USER" "$UV_BIN" sync --extra cli
ok "Python venv ready (server + cli)"

# ---------------------------------------------------------------------------
# 9. Run migrations
# ---------------------------------------------------------------------------
step "Running database migrations"

export ALEMBIC_CONTROL_URL="${DB_URL}/${PG_DB}"
sudo -u "$SVC_USER" --preserve-env=ALEMBIC_CONTROL_URL \
  "$APP_DIR/.venv/bin/python" -m alembic -c alembic-control.ini upgrade head
ok "Control plane migrations applied"

# ---------------------------------------------------------------------------
# 10. systemd units
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
ExecStart=${APP_DIR}/.venv/bin/uvicorn src.server.api.main:app --host \${API_LISTEN_HOST} --port 8000 --workers 2
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
ExecStart=${APP_DIR}/.venv/bin/lumiverb pipeline
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

cat > /etc/systemd/system/lumiverb-upkeep.service <<UNIT
[Unit]
Description=Lumiverb periodic upkeep (search sync, cleanup)

[Service]
Type=oneshot
User=${SVC_USER}
Group=${SVC_USER}
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/curl -sf -X POST http://127.0.0.1:8000/v1/upkeep -H "Authorization: Bearer \${ADMIN_KEY}" -H "Content-Type: application/json"
TimeoutSec=120
UNIT

cat > /etc/systemd/system/lumiverb-upkeep.timer <<UNIT
[Unit]
Description=Run Lumiverb upkeep every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=30s

[Install]
WantedBy=timers.target
UNIT

cat > /etc/systemd/system/lumiverb-upkeep-daily.service <<UNIT
[Unit]
Description=Lumiverb daily maintenance (filesystem cleanup)

[Service]
Type=oneshot
User=${SVC_USER}
Group=${SVC_USER}
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/curl -sf -X POST "http://127.0.0.1:8000/v1/upkeep/cleanup?dry_run=false" -H "Authorization: Bearer \${ADMIN_KEY}" -H "Content-Type: application/json"
TimeoutSec=300
UNIT

cat > /etc/systemd/system/lumiverb-upkeep-daily.timer <<UNIT
[Unit]
Description=Run Lumiverb daily maintenance at 3am

[Timer]
OnCalendar=*-*-* 03:00:00
AccuracySec=5min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
ok "systemd units installed"

# ---------------------------------------------------------------------------
# 11. Firewall
# ---------------------------------------------------------------------------
step "Configuring firewall (ufw)"

ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp   # SSH

# Open API port for remote web server / CLI access
if [[ -n "$API_ALLOW_FROM" ]]; then
  ufw allow from "$API_ALLOW_FROM" to any port 8000 proto tcp
  ok "Port 8000 open from ${API_ALLOW_FROM}"
elif [[ "$API_LISTEN_HOST" != "127.0.0.1" ]]; then
  ufw allow 8000/tcp
  ok "Port 8000 open to all (use --api-allow-from to restrict)"
fi

ufw --force enable
ok "Firewall active"

# ---------------------------------------------------------------------------
# 12. Start services
# ---------------------------------------------------------------------------
step "Starting services"

systemctl enable --now lumiverb-quickwit lumiverb-api lumiverb-upkeep.timer lumiverb-upkeep-daily.timer

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

systemctl status --no-pager lumiverb-quickwit lumiverb-api || true

# ---------------------------------------------------------------------------
# 13. Provision default tenant
# ---------------------------------------------------------------------------
step "Provisioning tenant: ${TENANT_NAME}"

ADMIN_KEY="$(grep '^ADMIN_KEY=' "${ENV_FILE}" | cut -d= -f2-)"
TENANT_BODY="{\"name\": \"${TENANT_NAME}\", \"email\": \"${CERTBOT_EMAIL}\""
if [[ -n "$VISION_API_URL" ]]; then
  TENANT_BODY="${TENANT_BODY}, \"vision_api_url\": \"${VISION_API_URL}\""
fi
if [[ -n "$VISION_API_KEY" ]]; then
  TENANT_BODY="${TENANT_BODY}, \"vision_api_key\": \"${VISION_API_KEY}\""
fi
TENANT_BODY="${TENANT_BODY}}"

TENANT_RESPONSE="$(curl -sf -X POST http://127.0.0.1:8000/v1/admin/tenants \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -H "Content-Type: application/json" \
  -d "${TENANT_BODY}")" \
  || fail "Tenant provisioning failed — check: journalctl -u lumiverb-api -n 50"

TENANT_ID="$(echo "$TENANT_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['tenant_id'])")"
TENANT_API_KEY="$(echo "$TENANT_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])")"
ok "Tenant ${TENANT_ID} provisioned"

sudo -u "${SVC_USER}" mkdir -p "${SVC_HOME}/.lumiverb"
echo "{\"api_url\": \"http://127.0.0.1:8000\", \"api_key\": \"${TENANT_API_KEY}\"}" \
  | sudo -u "${SVC_USER}" tee "${SVC_HOME}/.lumiverb/config.json" > /dev/null
ok "CLI configured for tenant"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}Lumiverb API deployed.${NC}"
echo ""
echo "API listening on ${API_LISTEN_HOST}:8000"
echo ""
echo "Next steps:"
echo ""
echo "  1. Create the first admin user:"
EXAMPLE_EMAIL="${CERTBOT_EMAIL:-you@example.com}"
echo "     sudo -u lumiverb /opt/lumiverb/.venv/bin/lumiverb user create --email ${EXAMPLE_EMAIL} --role admin"
echo ""
echo "  2. Deploy the web UI (same or different machine):"
echo "     bash scripts/deploy-web.sh --domain app.lumiverb.io --api-upstream http://${API_LISTEN_HOST}:8000"
echo ""
echo "Useful commands:"
echo "  journalctl -u lumiverb-api -f          # API logs"
echo "  journalctl -u lumiverb-worker -f       # Worker logs"
echo "  systemctl restart lumiverb-api          # Restart API"
echo ""
echo "Config: ${ENV_FILE} (contains secrets — use 'sudo cat' with care)"
echo ""
