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
# The brain (ADR-016 phase 2), a LAN/Tailscale box that shares its ports
# with other services and runs the worker:
#   sudo bash scripts/deploy-api.sh --app-host http://192.168.86.166 \
#     --pg-port 5434 --api-port 8100 --quickwit-port 7290 --no-firewall \
#     --data-dir /mnt/ssd2/lumiverb --worker \
#     --root-map /Volumes/media-01=/mnt/media-01 --branch feat/brain
#
# Idempotent: safe to run again to update an existing install. Ports, the
# app host, the data dir, the branch, the Postgres version and --no-firewall
# given once are remembered in /etc/lumiverb/env. --dry-run prints what a
# run would use and changes nothing.
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
BRANCH=""
CERTBOT_EMAIL=""
TENANT_NAME="Lumiverb"
DATA_DIR_OVERRIDE=""
VISION_API_URL=""
VISION_API_KEY=""
API_LISTEN_HOST=""
API_ALLOW_FROM=""
APP_HOST=""
PG_PORT=""
PG_VERSION=""
API_PORT=""
QW_PORT=""
NO_FIREWALL=false
WITH_WORKER=false
DRY_RUN=false
ROOT_MAPS=()

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
    --app-host)         APP_HOST="${2:?Missing value for --app-host}"; shift 2 ;;
    --pg-port)          PG_PORT="${2:?Missing value for --pg-port}"; shift 2 ;;
    --pg-version)       PG_VERSION="${2:?Missing value for --pg-version}"; shift 2 ;;
    --api-port)         API_PORT="${2:?Missing value for --api-port}"; shift 2 ;;
    --quickwit-port)    QW_PORT="${2:?Missing value for --quickwit-port}"; shift 2 ;;
    --no-firewall)      NO_FIREWALL=true; shift ;;
    --worker)           WITH_WORKER=true; shift ;;
    --root-map)         ROOT_MAPS+=("${2:?Missing value for --root-map}"); shift 2 ;;
    --dry-run)          DRY_RUN=true; shift ;;
    -h|--help)
      echo "Usage: $0 (--domain <FQDN> | --app-host <URL>) [--email <email>] [--tenant <name>] [--data-dir <path>]"
      echo "          [--api-listen-host <ip>] [--api-allow-from <cidr>] [--vision-api-url <url>] [--vision-api-key <key>]"
      echo "          [--pg-port <port>] [--pg-version <major>] [--api-port <port>] [--quickwit-port <port>]"
      echo "          [--no-firewall] [--worker] [--root-map <server-prefix>=<local-prefix>]... [--repo <url>] [--branch <ref>] [--dry-run]"
      echo ""
      echo "  --app-host       Base URL people reach Lumiverb at, instead of https://<domain> (e.g. http://192.168.86.166)"
      echo "  --no-firewall    Leave ufw alone (the host runs other services)"
      echo "  --worker         Install and start lumiverb-worker: scan and enrich on this machine (ffmpeg, Whisper, ...)"
      echo "  --root-map       Where a library root prefix is on this machine, e.g. /Volumes/media-01=/mnt/media-01"
      echo "  --dry-run        Print the settings this run would use (flags, else values remembered from"
      echo "                   an earlier run, else defaults) and stop. Changes nothing."
      exit 0
      ;;
    *) fail "Unknown option: $1" ;;
  esac
done

# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------
if [[ "$DOMAIN" == *"example.com"* ]]; then
  fail "Replace example.com with your actual domain"
fi
for map in "${ROOT_MAPS[@]}"; do
  [[ "$map" == /*=/* ]] || fail "--root-map takes <server-prefix>=<local-prefix>, both absolute: $map"
done

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
APP_DIR="/opt/lumiverb"
CONF_DIR="${LUMIVERB_CONF_DIR:-/etc/lumiverb}"
BACKUP_DIR="/var/backups/lumiverb"
ENV_FILE="${CONF_DIR}/env"
SVC_USER="lumiverb"
PG_USER="app"
PG_DB="control_plane"
UV_VERSION="0.7.12"
UV_BIN="/usr/local/bin/uv"
QUICKWIT_VERSION="0.8.2"

# Flag > value remembered from an earlier run > default.
_existing_val() {
  grep "^${1}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true
}
PG_PORT="${PG_PORT:-$(_existing_val PG_PORT)}";        PG_PORT="${PG_PORT:-5432}"
API_PORT="${API_PORT:-$(_existing_val API_PORT)}";     API_PORT="${API_PORT:-8000}"
QW_PORT="${QW_PORT:-$(_existing_val QUICKWIT_PORT)}";  QW_PORT="${QW_PORT:-7280}"
APP_HOST="${APP_HOST:-$(_existing_val APP_HOST)}"
[[ -n "$DOMAIN" || -n "$APP_HOST" ]] || fail "Required: --domain <FQDN> (e.g. --domain api.example.com) or --app-host <URL>"
APP_HOST="${APP_HOST:-https://${DOMAIN}}"
QW_GRPC_PORT=$((QW_PORT + 1))
# A rerun without --data-dir must not move storage back to the default:
# search would come up empty and every artifact would 404.
DATA_DIR="${DATA_DIR_OVERRIDE:-$(_existing_val DATA_DIR)}"; DATA_DIR="${DATA_DIR:-/var/lib/lumiverb}"
BRANCH="${BRANCH:-$(_existing_val BRANCH)}";             BRANCH="${BRANCH:-main}"
[[ "$NO_FIREWALL" == "true" ]] || NO_FIREWALL="$(_existing_val NO_FIREWALL)"
[[ "$NO_FIREWALL" == "true" ]] || NO_FIREWALL=false
# Postgres: the version an earlier install uses; new installs get 18, which
# Ubuntu 26.04 ships with pgvector (tests run against it too). Installs from
# before the version was remembered use the cluster on their port.
PG_VERSION="${PG_VERSION:-$(_existing_val PG_VERSION)}"
if [[ -z "$PG_VERSION" && -f "$ENV_FILE" ]]; then
  PG_VERSION="$(pg_lsclusters -h 2>/dev/null | awk -v p="$PG_PORT" '$3 == p { print $1; exit }' || true)"
fi
PG_VERSION="${PG_VERSION:-18}"

if [[ "$DRY_RUN" == "true" ]]; then
  echo "APP_HOST=${APP_HOST}"
  echo "DATA_DIR=${DATA_DIR}"
  echo "BRANCH=${BRANCH}"
  echo "NO_FIREWALL=${NO_FIREWALL}"
  echo "PG_VERSION=${PG_VERSION}"
  echo "PG_PORT=${PG_PORT}"
  echo "API_PORT=${API_PORT}"
  echo "QUICKWIT_PORT=${QW_PORT}"
  echo "WORKER=${WITH_WORKER}"
  echo "ROOT_MAPS=${ROOT_MAPS[*]-}"
  exit 0
fi

[[ "$(id -u)" -eq 0 ]] || fail "This script must be run as root (try: sudo bash ...)"
mkdir -p "$DATA_DIR" || fail "Cannot create data directory: $DATA_DIR"

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

# PostgreSQL: the distribution's packages when it has them, else the PGDG repo.
if ! apt-cache show "postgresql-${PG_VERSION}-pgvector" >/dev/null 2>&1; then
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /usr/share/keyrings/postgresql.gpg
  echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update -qq
fi

# Create the cluster ourselves, on PG_PORT: left to itself the package
# creates one on 5432, which may belong to something else (DaVinci
# Resolve's database on the brain).
if [[ ! -d "/etc/postgresql/${PG_VERSION}/main" ]]; then
  mkdir -p /etc/postgresql-common/createcluster.d
  echo "create_main_cluster = false" > /etc/postgresql-common/createcluster.d/lumiverb.conf
fi

apt-get install -y -qq \
  "postgresql-${PG_VERSION}" "postgresql-${PG_VERSION}-pgvector" \
  git build-essential python3-dev
[[ "$NO_FIREWALL" == "true" ]] || apt-get install -y -qq ufw

if [[ "$WITH_WORKER" == "true" ]]; then
  # Scan and enrich: video, EXIF, image decoding.
  apt-get install -y -qq ffmpeg libimage-exiftool-perl
  apt-get install -y -qq libvips42t64 2>/dev/null || apt-get install -y -qq libvips42
fi

ok "System packages installed (PostgreSQL ${PG_VERSION})"

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

if [[ ! -d "/etc/postgresql/${PG_VERSION}/main" ]]; then
  pg_createcluster "$PG_VERSION" main --port="$PG_PORT" --start
fi

PG_CONF="/etc/postgresql/${PG_VERSION}/main/postgresql.conf"
PG_NEEDS_RESTART=false
current_port=$(grep -oP "^\s*port\s*=\s*\K[0-9]+" "$PG_CONF" || true)
if [[ "$current_port" != "$PG_PORT" ]]; then
  if grep -qE "^\s*port\s*=" "$PG_CONF"; then
    sed -i "s/^\s*port\s*=.*/port = ${PG_PORT}/" "$PG_CONF"
  else
    echo "port = ${PG_PORT}" >> "$PG_CONF"
  fi
  PG_NEEDS_RESTART=true
fi
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
  systemctl restart "postgresql@${PG_VERSION}-main" 2>/dev/null || systemctl restart postgresql
fi
systemctl start "postgresql@${PG_VERSION}-main" 2>/dev/null || true
PSQL="psql -p ${PG_PORT}"
for i in {1..15}; do
  su - postgres -c "${PSQL} -tc 'SELECT 1'" >/dev/null 2>&1 && break
  sleep 1
done

su - postgres -c "${PSQL} -tc \"SELECT 1 FROM pg_roles WHERE rolname='${PG_USER}'\"" | grep -q 1 \
  || su - postgres -c "${PSQL} -c \"CREATE USER ${PG_USER} WITH PASSWORD '${PG_PASS}' CREATEDB\""

su - postgres -c "${PSQL} -c \"ALTER USER ${PG_USER} WITH PASSWORD '${PG_PASS}' CREATEDB\""

su - postgres -c "${PSQL} -tc \"SELECT 1 FROM pg_database WHERE datname='${PG_DB}'\"" | grep -q 1 \
  || su - postgres -c "${PSQL} -c \"CREATE DATABASE ${PG_DB} OWNER ${PG_USER}\""

su - postgres -c "${PSQL} -d template1 -c 'CREATE EXTENSION IF NOT EXISTS vector'"
su - postgres -c "${PSQL} -d ${PG_DB} -c 'CREATE EXTENSION IF NOT EXISTS vector'"

ok "PostgreSQL: user=${PG_USER}, db=${PG_DB}, pgvector enabled"

# ---------------------------------------------------------------------------
# 5. Generate secrets and write env file
# ---------------------------------------------------------------------------
step "Writing ${ENV_FILE}"

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

DB_URL="postgresql+psycopg2://${PG_USER}:${PG_PASS}@127.0.0.1:${PG_PORT}"

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
QUICKWIT_URL=http://127.0.0.1:${QW_PORT}
QUICKWIT_ENABLED=true

# API
API_LISTEN_HOST=${API_LISTEN_HOST}
API_PORT=${API_PORT}

# What this install uses (remembered for reruns)
PG_PORT=${PG_PORT}
PG_VERSION=${PG_VERSION}
QUICKWIT_PORT=${QW_PORT}
BRANCH=${BRANCH}
NO_FIREWALL=${NO_FIREWALL}

# App
APP_ENV=production
APP_HOST=${APP_HOST}
LOG_LEVEL=INFO

# Password reset SMTP (uncomment and fill in to enable forgot-password)
# SMTP_HOST=smtp.example.com
# SMTP_PORT=587
# SMTP_USER=apikey
# SMTP_PASSWORD=
# SMTP_FROM=noreply@${DOMAIN:-lumiverb.local}
ENVEOF

chmod 600 "$ENV_FILE"
ok "Secrets generated, env written (API on ${API_LISTEN_HOST}:${API_PORT}, Postgres on ${PG_PORT}, Quickwit on ${QW_PORT})"

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
  listen_port: ${QW_PORT}
grpc_listen_port: ${QW_GRPC_PORT}
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
  # The checkout belongs to $SVC_USER; tell git it's safe for root.
  git config --system --replace-all safe.directory "$APP_DIR" "$APP_DIR" 2>/dev/null \
    || git config --global --add safe.directory "$APP_DIR"
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

EXTRAS=(--extra cli --extra embeddings --extra face_recognition)
if [[ "$WITH_WORKER" == "true" ]] || systemctl is-enabled lumiverb-worker >/dev/null 2>&1; then
  WITH_WORKER=true
  EXTRAS+=(--extra workers)
fi
sudo -u "$SVC_USER" "$UV_BIN" sync "${EXTRAS[@]}"
ok "Python venv ready (${EXTRAS[*]})"

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
ExecStart=${APP_DIR}/.venv/bin/uvicorn src.server.api.main:app --host \${API_LISTEN_HOST} --port \${API_PORT} --workers 2
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

# Scan what changed and enrich what's missing (ADR-016 phase 2). Everything
# outside DATA_DIR and its home is read-only to it, storage mounts included.
cat > /etc/systemd/system/lumiverb-worker.service <<UNIT
[Unit]
Description=Lumiverb worker (scan and enrich)
After=network-online.target remote-fs.target lumiverb-api.service
Wants=network-online.target lumiverb-api.service

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
Environment=HOME=${SVC_HOME}
# Proxy caches go on the data disk, not the root disk with Postgres.
Environment=XDG_CACHE_HOME=${DATA_DIR}/cache
ExecStart=${APP_DIR}/.venv/bin/lumiverb worker
Restart=on-failure
RestartSec=30s
TimeoutStopSec=30s
LimitNOFILE=65535
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${DATA_DIR} ${SVC_HOME}

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
ExecStart=/usr/bin/curl -sf -X POST http://127.0.0.1:\${API_PORT}/v1/upkeep -H "Authorization: Bearer \${ADMIN_KEY}" -H "Content-Type: application/json"
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
ExecStart=/usr/bin/curl -sf -X POST "http://127.0.0.1:\${API_PORT}/v1/upkeep/cleanup?dry_run=false" -H "Authorization: Bearer \${ADMIN_KEY}" -H "Content-Type: application/json"
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
if [[ "$NO_FIREWALL" == "true" ]]; then
  step "Firewall"
  ok "Left alone (--no-firewall)"
else
step "Configuring firewall (ufw)"

ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp   # SSH

# Open API port for remote web server / CLI access
if [[ -n "$API_ALLOW_FROM" ]]; then
  ufw allow from "$API_ALLOW_FROM" to any port "$API_PORT" proto tcp
  ok "Port ${API_PORT} open from ${API_ALLOW_FROM}"
elif [[ "$API_LISTEN_HOST" != "127.0.0.1" ]]; then
  ufw allow "${API_PORT}/tcp"
  ok "Port ${API_PORT} open to all (use --api-allow-from to restrict)"
fi

ufw --force enable
ok "Firewall active"
fi

# ---------------------------------------------------------------------------
# 12. Start services
# ---------------------------------------------------------------------------
step "Starting services"

systemctl enable --now lumiverb-quickwit lumiverb-api lumiverb-upkeep.timer lumiverb-upkeep-daily.timer

for i in {1..10}; do
  if curl -sf http://127.0.0.1:${API_PORT}/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if curl -sf http://127.0.0.1:${API_PORT}/health >/dev/null 2>&1; then
  ok "API server healthy"
else
  warn "API server not responding yet — check: journalctl -u lumiverb-api -n 50"
fi

systemctl status --no-pager lumiverb-quickwit lumiverb-api || true

# ---------------------------------------------------------------------------
# 13. Provision default tenant (first run only)
# ---------------------------------------------------------------------------
CLI_CONFIG="${SVC_HOME}/.lumiverb/config.json"
if [[ -f "$CLI_CONFIG" ]] && grep -q '"api_key": *"[^"]' "$CLI_CONFIG"; then
  step "Tenant"
  ok "Already provisioned (${CLI_CONFIG} has a key); not creating another"
  # Keep the service user's CLI pointed at this install's API port.
  sudo -u "${SVC_USER}" -H python3 - "$CLI_CONFIG" "$API_PORT" <<'PY'
import json, sys
path, port = sys.argv[1], sys.argv[2]
cfg = json.load(open(path))
cfg["api_url"] = f"http://127.0.0.1:{port}"
json.dump(cfg, open(path, "w"), indent=2)
PY
else
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

TENANT_RESPONSE="$(curl -sf -X POST http://127.0.0.1:${API_PORT}/v1/admin/tenants \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -H "Content-Type: application/json" \
  -d "${TENANT_BODY}")" \
  || fail "Tenant provisioning failed — check: journalctl -u lumiverb-api -n 50"

TENANT_ID="$(echo "$TENANT_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['tenant_id'])")"
TENANT_API_KEY="$(echo "$TENANT_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])")"
ok "Tenant ${TENANT_ID} provisioned"

sudo -u "${SVC_USER}" mkdir -p "${SVC_HOME}/.lumiverb"
echo "{\"api_url\": \"http://127.0.0.1:${API_PORT}\", \"api_key\": \"${TENANT_API_KEY}\"}" \
  | sudo -u "${SVC_USER}" tee "$CLI_CONFIG" > /dev/null
chmod 600 "$CLI_CONFIG"
ok "CLI configured for tenant"
fi

# ---------------------------------------------------------------------------
# 14. Worker: where library roots are on this machine, then start it
# ---------------------------------------------------------------------------
for map in "${ROOT_MAPS[@]}"; do
  sudo -u "${SVC_USER}" -H "${APP_DIR}/.venv/bin/lumiverb" config map-root "${map%%=*}" "${map#*=}"
done
if [[ "$WITH_WORKER" == "true" ]]; then
  step "Starting the worker"
  systemctl enable lumiverb-worker
  systemctl restart lumiverb-worker
  ok "lumiverb-worker running (journalctl -u lumiverb-worker -f)"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}Lumiverb API deployed.${NC}"
echo ""
echo "API listening on ${API_LISTEN_HOST}:${API_PORT}; people reach it at ${APP_HOST}"
echo ""
echo "Next steps:"
echo ""
echo "  1. Create the first admin user:"
EXAMPLE_EMAIL="${CERTBOT_EMAIL:-you@example.com}"
echo "     sudo -u lumiverb -H /opt/lumiverb/.venv/bin/lumiverb user create --email ${EXAMPLE_EMAIL} --role admin"
echo ""
echo "  2. Deploy the web UI (same or different machine):"
echo "     bash scripts/deploy-web.sh --domain app.lumiverb.io --api-upstream http://${API_LISTEN_HOST}:${API_PORT}"
echo ""
echo "Useful commands:"
echo "  journalctl -u lumiverb-api -f          # API logs"
echo "  journalctl -u lumiverb-worker -f       # Worker logs"
echo "  systemctl restart lumiverb-api          # Restart API"
echo ""
echo "Config: ${ENV_FILE} (contains secrets — use 'sudo cat' with care)"
echo ""
