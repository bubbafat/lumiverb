#!/usr/bin/env bash
# Bootstrap the Lumiverb API server on a fresh Ubuntu 24.04+ machine (22.04's libvips is too old to process).
#
# Installs PostgreSQL, Python/uv, runs migrations, creates systemd units.
# Search is PostgreSQL's own (ADR-019): there's no search service. Does NOT
# install nginx, Node.js, or build the web UI — use deploy-web.sh for that
# (same or different machine).
#
# Usage:
#   bash scripts/deploy-api.sh --domain api.example.com
#
# Prod (ADR-016 phase 2), a LAN/Tailscale box that shares its ports
# with other services and runs the scheduler (all processing):
#   sudo bash scripts/deploy-api.sh --app-host http://192.168.86.166 \
#     --pg-port 5434 --api-port 8100 --no-firewall \
#     --data-dir /mnt/ssd2/lumiverb --worker \
#     --root-map /Volumes/media-01=/mnt/media-01
#
# Idempotent: safe to run again to update an existing install. Ports, the
# app host, the data dir, the branch, the Postgres version and --no-firewall
# given once are remembered in /etc/lumiverb/env (--firewall undoes
# --no-firewall). --dry-run prints what a run would use and changes nothing.
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
REPO_URL="https://github.com/bubbafat/lumiverb.git"
BRANCH=""
CERTBOT_EMAIL=""
TENANT_NAME="Lumiverb"
DATA_DIR_OVERRIDE=""
VISION_API_URL=""
VISION_API_KEY=""
VISION_API_KEY_FILE=""
API_LISTEN_HOST=""
API_ALLOW_FROM=""
APP_HOST=""
PG_PORT=""
PG_VERSION=""
API_PORT=""
NO_FIREWALL=""
EXPOSURE=""
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
    --vision-api-key)   VISION_API_KEY="${2:?Missing value for --vision-api-key}"; shift 2
                        warn "--vision-api-key puts the key on this command's line, which every local user can see (ps) while the install runs, and in your shell's history. Use --vision-api-key-file FILE or LUMIVERB_VISION_API_KEY instead, or leave it out to be asked for it." >&2 ;;
    --vision-api-key-file) VISION_API_KEY_FILE="${2:?Missing value for --vision-api-key-file}"; shift 2 ;;
    --api-listen-host)  API_LISTEN_HOST="${2:?Missing value for --api-listen-host}"; shift 2 ;;
    --api-allow-from)   API_ALLOW_FROM="${2:?Missing value for --api-allow-from}"; shift 2 ;;
    --app-host)         APP_HOST="${2:?Missing value for --app-host}"; shift 2 ;;
    --pg-port)          PG_PORT="${2:?Missing value for --pg-port}"; shift 2 ;;
    --pg-version)       PG_VERSION="${2:?Missing value for --pg-version}"; shift 2 ;;
    --api-port)         API_PORT="${2:?Missing value for --api-port}"; shift 2 ;;
    --no-firewall)      NO_FIREWALL=true; shift ;;
    --firewall)         NO_FIREWALL=false; shift ;;
    --worker)           WITH_WORKER=true; shift ;;
    --exposure)         EXPOSURE="${2:?Missing value for --exposure}"; shift 2 ;;
    --root-map)         ROOT_MAPS+=("${2:?Missing value for --root-map}"); shift 2 ;;
    --dry-run)          DRY_RUN=true; shift ;;
    -h|--help)
      echo "Usage: $0 (--domain <FQDN> | --app-host <URL>) [--email <email>] [--tenant <name>] [--data-dir <path>]"
      echo "          [--api-listen-host <ip>] [--api-allow-from <cidr>] [--vision-api-url <url> [--vision-api-key-file <file>]]"
      echo "          [--pg-port <port>] [--pg-version <major>] [--api-port <port>]"
      echo "          [--no-firewall | --firewall] [--worker] [--root-map <server-prefix>=<local-prefix>]... [--exposure demo]"
      echo "          [--repo <url>] [--branch <ref>] [--dry-run]"
      echo ""
      echo "  --app-host       Base URL people reach Lumiverb at, instead of https://<domain> (e.g. http://192.168.86.166)"
      echo "  --no-firewall    Leave ufw alone (the host runs other services); remembered"
      echo "  --firewall       Turn ufw on again after --no-firewall"
      echo "  --worker         Install and start lumiverb-scheduler: all processing on this machine (ffmpeg, Whisper, ...)"
      echo "  --root-map       Where a library root prefix is on this machine, e.g. /Volumes/media-01=/mnt/media-01"
      echo "  --exposure demo  From the start, answer from anywhere but this machine only what a demo's visitors"
      echo "                   call (docs/demo-server.md); remembered"
      echo "  --vision-api-url The first account's AI machine doing vision (made with the account, on the first run)"
      echo "  --vision-api-key-file  Its key, the file's first line. Or LUMIVERB_VISION_API_KEY (sudo"
      echo "                   --preserve-env=LUMIVERB_VISION_API_KEY), or typed when asked, on a terminal."
      echo "                   (--vision-api-key <key> still works, warned against: a command's arguments are"
      echo "                   every local user's to see.)"
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
[[ -z "$VISION_API_KEY" || -z "$VISION_API_KEY_FILE" ]] || fail "Give --vision-api-key-file or --vision-api-key, not both"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
APP_DIR="/opt/lumiverb"
CONF_DIR="${LUMIVERB_CONF_DIR:-/etc/lumiverb}"
BACKUP_DIR="/var/backups/lumiverb"
ENV_FILE="${CONF_DIR}/env"
SVC_USER="lumiverb"
SVC_HOME="/var/lib/lumiverb"
PG_USER="app"
PG_DB="control_plane"
UV_VERSION="0.7.12"
UV_BIN="/usr/local/bin/uv"

# The service user's CLI config: the first account is made only while it holds no key (step 12). Read as that user:
# root opens nothing in its home.
CLI_CONFIG="${SVC_HOME}/.lumiverb/config.json"
account_made() {
  id -u "$SVC_USER" >/dev/null 2>&1 && sudo -u "${SVC_USER}" grep -qs '"api_key": *"[^"]' -- "$CLI_CONFIG"
}

# What root runs as the service user reads none of that user's own settings: git's (~/.gitconfig, which can name
# hooks and an fsmonitor to run), npm's (~/.npmrc) and uv's (~/.config/uv/uv.toml). They're the service user's to
# write, and what they name would run outside the services' sandbox. Its home and caches stay as they were.
SVC_ENV=(env GIT_CONFIG_GLOBAL=/dev/null npm_config_userconfig=/dev/null UV_NO_CONFIG=1)
as_svc() { sudo -u "$SVC_USER" "${SVC_ENV[@]}" "$@"; }

# A folder in DATA_DIR for the service user, made and handed over by root. The service user owns DATA_DIR, so it
# could have put a symbolic link where the folder goes, which mkdir and chown would follow: any link stops it
# before a folder is made or handed over. chown -h changes the folder itself, never what a link put there since
# points at. Held to the same text in deploy-api.sh and update-api.sh.
svc_folder() {
  local dir
  for dir in "$@"; do
    [[ ! -L "$dir" ]] || fail "${dir} is a symbolic link where Lumiverb keeps a folder: remove the link (sudo rm ${dir}), then run this again"
  done
  for dir in "$@"; do
    mkdir -p -- "$dir"
    chown -h "$SVC_USER":"$SVC_USER" -- "$dir"
  done
}

# DATA_DIR, which goes into units and what root does: one line, a full path, nothing a unit file or a shell would
# read as more than a path (empty: there's none). Held to the same text in deploy-api.sh and update-api.sh.
data_dir_shape() {
  [[ -z "$1" ]] || [[ "$1" == /* && "$1" != *[[:space:][:cntrl:]\"\'\`\$%\;\\]* ]] \
    || fail "DATA_DIR must be one full path, without spaces, quotes or any of \$ % ; \\: $(printf '%q' "$1")"
}

# The env file is root's (600, in a 750 folder): read as anyone else it
# looks absent, and the defaults would stand in for what's remembered.
if [[ -e "$ENV_FILE" && ! -r "$ENV_FILE" ]] || [[ -d "$CONF_DIR" && ! -x "$CONF_DIR" ]]; then
  fail "Run with sudo to see the settings remembered in ${ENV_FILE}"
fi

# Flag > value remembered from an earlier run > default.
_existing_val() {
  grep "^${1}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true
}
PG_PORT="${PG_PORT:-$(_existing_val PG_PORT)}";        PG_PORT="${PG_PORT:-5432}"
API_PORT="${API_PORT:-$(_existing_val API_PORT)}";     API_PORT="${API_PORT:-8000}"
API_LISTEN_HOST="${API_LISTEN_HOST:-$(_existing_val API_LISTEN_HOST)}"; API_LISTEN_HOST="${API_LISTEN_HOST:-127.0.0.1}"
APP_HOST="${APP_HOST:-$(_existing_val APP_HOST)}"
[[ -n "$DOMAIN" || -n "$APP_HOST" ]] || fail "Required: --domain <FQDN> (e.g. --domain api.example.com) or --app-host <URL>"
APP_HOST="${APP_HOST:-https://${DOMAIN}}"
# A rerun without --data-dir must not move storage back to the default:
# every preview and artifact would 404.
DATA_DIR="${DATA_DIR_OVERRIDE:-$(_existing_val DATA_DIR)}"; DATA_DIR="${DATA_DIR:-/var/lib/lumiverb}"
data_dir_shape "$DATA_DIR"
BRANCH="${BRANCH:-$(_existing_val BRANCH)}";             BRANCH="${BRANCH:-main}"
NO_FIREWALL="${NO_FIREWALL:-$(_existing_val NO_FIREWALL)}"
[[ "$NO_FIREWALL" == "true" ]] || NO_FIREWALL=false
EXPOSURE="${EXPOSURE:-$(_existing_val LUMIVERB_EXPOSURE)}"
# The exposures there are (src/server/api/exposure.py EXPOSURES).
case "$EXPOSURE" in
  ""|demo) ;;
  *) fail "--exposure (LUMIVERB_EXPOSURE) takes demo: $EXPOSURE" ;;
esac
# Postgres: the version an earlier install uses; new installs get 18, which
# Ubuntu 26.04 ships with pgvector (tests run against it too). Installs from
# before the version was remembered use the cluster on their port.
PG_VERSION="${PG_VERSION:-$(_existing_val PG_VERSION)}"
if [[ -z "$PG_VERSION" && -f "$ENV_FILE" ]]; then
  PG_VERSION="$(pg_lsclusters -h 2>/dev/null | awk -v p="$PG_PORT" '$3 == p { print $1; exit }' || true)"
fi
PG_VERSION="${PG_VERSION:-18}"
# Installed processing stays, with its packages, without --worker (the
# scheduler, or the worker it replaced).
if [[ "$WITH_WORKER" != "true" ]] && { systemctl is-enabled lumiverb-scheduler >/dev/null 2>&1 \
    || systemctl is-enabled lumiverb-worker >/dev/null 2>&1; }; then
  WITH_WORKER=true
fi

if [[ "$DRY_RUN" == "true" ]]; then
  echo "APP_HOST=${APP_HOST}"
  echo "DATA_DIR=${DATA_DIR}"
  echo "BRANCH=${BRANCH}"
  echo "NO_FIREWALL=${NO_FIREWALL}"
  echo "PG_VERSION=${PG_VERSION}"
  echo "PG_PORT=${PG_PORT}"
  echo "API_PORT=${API_PORT}"
  echo "API_LISTEN_HOST=${API_LISTEN_HOST}"
  echo "WORKER=${WITH_WORKER}"
  echo "EXPOSURE=${EXPOSURE}"
  echo "ROOT_MAPS=${ROOT_MAPS[*]-}"
  exit 0
fi

[[ "$(id -u)" -eq 0 ]] || fail "This script must be run as root (try: sudo bash ...)"

# ---------------------------------------------------------------------------
# The first account's vision key, never an argument
# ---------------------------------------------------------------------------
# The key of the AI machine --vision-api-url names, used once, when the first account is made (step 12). A command's
# arguments are every local user's to see (ps) while it runs, and stay in shell history: the key comes from a file
# (--vision-api-key-file, its first line), from LUMIVERB_VISION_API_KEY, or typed when asked, on a terminal, now,
# before the long steps. (--vision-api-key still works, said against as it's read.)
if [[ -n "$VISION_API_KEY_FILE" ]]; then
  [[ -r "$VISION_API_KEY_FILE" && ! -d "$VISION_API_KEY_FILE" ]] \
    || fail "Can't read ${VISION_API_KEY_FILE} (--vision-api-key-file)"
  IFS= read -r VISION_API_KEY < "$VISION_API_KEY_FILE" || true
  VISION_API_KEY="${VISION_API_KEY%$'\r'}"
  [[ -n "$VISION_API_KEY" ]] || fail "${VISION_API_KEY_FILE} (--vision-api-key-file) has no key on its first line"
fi
VISION_API_KEY="${VISION_API_KEY:-${LUMIVERB_VISION_API_KEY:-}}"
unset LUMIVERB_VISION_API_KEY  # nothing this starts gets it
if [[ -n "$VISION_API_URL" || -n "$VISION_API_KEY" ]] && account_made; then
  warn "The first account is made already, and --vision-api-url and its key are only for making it: add the machine in Settings › AI"
elif [[ -n "$VISION_API_URL" && -z "$VISION_API_KEY" && -t 0 ]]; then
  read -rsp "The key for ${VISION_API_URL} (not shown; Enter for none): " VISION_API_KEY || true
  echo
fi

# ---------------------------------------------------------------------------
# Before anything's made: where the data goes, and memory
# ---------------------------------------------------------------------------
FSTAB=/etc/fstab
MEMINFO=/proc/meminfo

# The deepest mount point /etc/fstab lists that holds the folder, but /.
fstab_mount_of() {
  local found="" device mount_point rest
  [[ -r "$FSTAB" ]] || return 0
  while read -r device mount_point rest; do
    [[ "$device" == \#* || "$mount_point" != /* || "$mount_point" == / ]] && continue
    if [[ "$1" == "$mount_point" || "$1" == "$mount_point"/* ]] && (( ${#mount_point} > ${#found} )); then
      found="$mount_point"
    fi
  done < "$FSTAB"
  echo "$found"
}

# Disks attached with nothing on them mounted (nor swap), and their sizes.
unmounted_disks() {
  lsblk -Jpo NAME,TYPE,SIZE,MOUNTPOINT 2>/dev/null | python3 -c '
import json, sys
def used(dev): return bool(dev.get("mountpoint")) or any(used(c) for c in dev.get("children") or [])
for dev in json.load(sys.stdin).get("blockdevices", []):
    if dev.get("type") == "disk" and not used(dev):
        print("%s (%s)" % (dev["name"], dev.get("size") or "?"))
' 2>/dev/null || true
}

# A drive that isn't there leaves a folder on the system disk in its place, and every preview, proxy and backup
# would go there without a word: the rule src/server/drives.py applies to a folder chosen later.
data_dir_check() {
  local dir="${1%/}" chosen="$2" mount_point there disks
  mount_point="$(fstab_mount_of "$dir")"
  if [[ -n "$mount_point" ]] && ! mountpoint -q "$mount_point"; then
    fail "There's no drive at ${mount_point} (/etc/fstab mounts one there). Mount it (sudo mount ${mount_point}) and run this again: otherwise the data folder ${dir} would be on the system disk."
  fi
  # A new folder chosen with --data-dir, on the system disk, while a disk sits unmounted: likely meant for it.
  [[ "$chosen" == true && ! -e "$dir" ]] || return 0
  there="$dir"
  while [[ ! -d "$there" ]]; do there="$(dirname "$there")"; done
  [[ "$(stat -c %d "$there")" == "$(stat -c %d /)" ]] || return 0
  disks="$(unmounted_disks)"
  disks="${disks//$'\n'/, }"
  [[ -z "$disks" ]] || fail "${dir} would be a new folder on the system disk, while a disk with nothing mounted is attached: ${disks}. If the data should go there, format and mount it (with a line in /etc/fstab, nofail) and run this again. To keep it on the system disk, make the folder first (sudo mkdir -p ${dir})."
}

# Little memory and no swap: processing here can need more than there is, and the kernel stops what runs out.
# Said first and again at the end.
LOW_MEMORY=""
memory_check() {
  local mem_kb swap_kb
  mem_kb="$(awk '/^MemTotal:/ { print $2 }' "$MEMINFO" 2>/dev/null || true)"
  swap_kb="$(awk '/^SwapTotal:/ { print $2 }' "$MEMINFO" 2>/dev/null || true)"
  if (( ${mem_kb:-0} > 0 && ${mem_kb:-0} < 4194304 && ${swap_kb:-0} == 0 )); then
    LOW_MEMORY="This machine has $(awk -v kb="$mem_kb" 'BEGIN { printf "%.1f", kb / 1048576 }') GB of memory and no swap. Finding faces, CLIP and Whisper's transcripts can need more than that at once, and the kernel stops what runs out: add swap or memory."
    warn "$LOW_MEMORY"
  fi
}

# Photos are read with libvips 8.13 or later, the first that can turn off its untrusted loaders:
# src/processing/libvips.py refuses an older one (TRUSTED_ONLY_SINCE), and scans stop, saying why. Ubuntu 22.04's
# is 8.12, 24.04's 8.15. Held to the same text in deploy-api.sh and update-api.sh.
LIBVIPS_NEEDED=8.13
libvips_too_old() {
  [[ -n "$1" ]] && dpkg --compare-versions "$1" lt "$LIBVIPS_NEEDED"
}

# The libvips processing would install here: one too old stops the install before any package is.
libvips_check() {
  local vips
  vips="$(apt-cache policy libvips42t64 libvips42 2>/dev/null \
    | awk '$1 == "Candidate:" && $2 != "(none)" { print $2; exit }' || true)"
  if libvips_too_old "$vips"; then
    fail "This system's libvips is ${vips%%-*}: Lumiverb reads photos with libvips ${LIBVIPS_NEEDED} or later, the first that can turn off its untrusted loaders. Install on Ubuntu 24.04 or later (or without --worker, for a server that doesn't process). No package was installed."
  fi
}
# ---------------------------------------------------------------------------

data_dir_check "$DATA_DIR" "$([[ -n "$DATA_DIR_OVERRIDE" ]] && echo true || echo false)"
memory_check
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
[[ "$WITH_WORKER" != "true" ]] || libvips_check

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
# Resolve's database on prod).
if [[ ! -d "/etc/postgresql/${PG_VERSION}/main" ]]; then
  mkdir -p /etc/postgresql-common/createcluster.d
  echo "create_main_cluster = false" > /etc/postgresql-common/createcluster.d/lumiverb.conf
fi

# python3-psycopg2: libpq's own SCRAM verifier of the app role's password, before Lumiverb's venv exists (step 4).
apt-get install -y -qq \
  "postgresql-${PG_VERSION}" "postgresql-${PG_VERSION}-pgvector" \
  git build-essential python3-dev python3-psycopg2
[[ "$NO_FIREWALL" == "true" ]] || apt-get install -y -qq ufw

# HEIC, an iPhone's photos: libvips reads them with libheif, whose HEVC decoder is a package of its own from
# Ubuntu 24.04 on (before, libheif has it built in). Installed where the distribution has it; a failure is said,
# never fatal (every other photo is read as before). Held to the same text in deploy-api.sh and update-api.sh.
heic_decoder() {
  local pkg=libheif-plugin-libde265
  if dpkg -s "$pkg" >/dev/null 2>&1; then
    ok "HEIC photos: ${pkg} is installed"
  elif ! apt-cache show "$pkg" >/dev/null 2>&1; then
    ok "HEIC photos: libheif decodes them itself here"
  elif apt-get install -y -qq "$pkg" >/dev/null 2>&1 \
      || { apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq "$pkg" >/dev/null 2>&1; }; then
    ok "HEIC photos: installed ${pkg}"
  else
    warn "HEIC photos can't be read until ${pkg} is installed: sudo apt-get install ${pkg}"
  fi
}

if [[ "$WITH_WORKER" == "true" ]]; then
  # Scan and enrich: video, EXIF, image decoding.
  apt-get install -y -qq ffmpeg libimage-exiftool-perl
  apt-get install -y -qq libvips42t64 2>/dev/null || apt-get install -y -qq libvips42
  heic_decoder
fi

ok "System packages installed (PostgreSQL ${PG_VERSION})"

# ---------------------------------------------------------------------------
# 2. uv (Python package manager)
# ---------------------------------------------------------------------------
step "Installing uv"

if [[ ! -x "$UV_BIN" ]]; then
  UV_ARCHIVE="uv-${RUST_TARGET}.tar.gz"
  # Downloaded into a folder of root's own, never to a fixed name in /tmp, where another user could have put a
  # file or a link first; gone when the install ends, however it ends.
  UV_TMP="$(mktemp -d)"
  trap 'rm -rf -- "$UV_TMP"' EXIT
  curl -LsSf "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${UV_ARCHIVE}" \
    -o "$UV_TMP/uv.tar.gz"
  curl -LsSf "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${UV_ARCHIVE}.sha256" \
    -o "$UV_TMP/uv.sha256"
  EXPECTED_HASH="$(awk '{print $1}' "$UV_TMP/uv.sha256")"
  ACTUAL_HASH="$(sha256sum "$UV_TMP/uv.tar.gz" | awk '{print $1}')"
  [[ "$EXPECTED_HASH" == "$ACTUAL_HASH" ]] || fail "uv checksum verification failed"
  tar -xzf "$UV_TMP/uv.tar.gz" -C "$UV_TMP"
  install -m 0755 "$UV_TMP/uv-${RUST_TARGET}/uv" "$UV_BIN"
  install -m 0755 "$UV_TMP/uv-${RUST_TARGET}/uvx" /usr/local/bin/uvx
  rm -rf -- "$UV_TMP"
  trap - EXIT
fi
ok "uv $($UV_BIN --version)"

# ---------------------------------------------------------------------------
# 3. Service user and directories
# ---------------------------------------------------------------------------
step "Creating service user and directories"

id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --shell /usr/sbin/nologin --home "$SVC_HOME" "$SVC_USER"

mkdir -p "$SVC_HOME" "$CONF_DIR" "$BACKUP_DIR"
chown -h "$SVC_USER":"$SVC_USER" "$SVC_HOME"
chown root:"$SVC_USER" "$CONF_DIR"
chmod 750 "$CONF_DIR"
svc_folder "$DATA_DIR"/tmp "$DATA_DIR"/worker-tmp
# -R changes links themselves, never what they point at, and never goes through one.
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

# The app role, made or put right, its password the one in .pg_password. PostgreSQL is sent libpq's SCRAM verifier of
# it, made here (psycopg2's encrypt_password), never the password: a statement holding it would be in PostgreSQL's
# log (log_statement) and in pg_stat_activity while it ran, and psql -c would have it on its command line, which every
# local user sees. Python has it on its stdin, and connects as postgres over the local socket, as psql does.
APP_ROLE_PY='
import sys
import psycopg2
from psycopg2 import sql
from psycopg2.extensions import encrypt_password

role, password = sys.argv[1], sys.stdin.read()
conn = psycopg2.connect(dbname="postgres")
conn.autocommit = True
with conn.cursor() as cur:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    verb = "ALTER" if cur.fetchone() else "CREATE"
    cur.execute(sql.SQL(verb + " USER {} CREATEDB PASSWORD {}").format(
        sql.Identifier(role), sql.Literal(encrypt_password(password, role, conn, "scram-sha-256"))))
conn.close()
'
printf '%s' "$PG_PASS" | sudo -u postgres env PGPORT="$PG_PORT" python3 -I -c "$APP_ROLE_PY" "$PG_USER"

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
LUMIVERB_SECRETS_KEY="$(_existing_val LUMIVERB_SECRETS_KEY)"

[[ -n "$ADMIN_KEY" ]]     || ADMIN_KEY="$(openssl rand -hex 32)"
[[ -n "$API_SECRET_KEY" ]] || API_SECRET_KEY="$(openssl rand -hex 32)"
[[ -n "$JWT_SECRET" ]]     || JWT_SECRET="$(openssl rand -hex 32)"
# A Fernet key (src/server/sealing.py): 32 random bytes, URL-safe base64.
[[ -n "$LUMIVERB_SECRETS_KEY" ]] || LUMIVERB_SECRETS_KEY="$(openssl rand -base64 32 | tr '+/' '-_')"

DB_URL="postgresql+psycopg2://${PG_USER}:${PG_PASS}@127.0.0.1:${PG_PORT}"

# Settings added by hand (SMTP_*, say): kept below what this script writes.
EARLIER="$(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE" 2>/dev/null || true)"
cat > "$ENV_FILE" <<ENVEOF
# Auto-generated by deploy-api.sh — $(date -u +%Y-%m-%dT%H:%M:%SZ)

# Database
CONTROL_PLANE_DATABASE_URL=${DB_URL}/${PG_DB}
TENANT_DATABASE_URL_TEMPLATE=${DB_URL}/{tenant_id}

# Auth
ADMIN_KEY=${ADMIN_KEY}
API_SECRET_KEY=${API_SECRET_KEY}
JWT_SECRET=${JWT_SECRET}
# Seals the secrets Lumiverb keeps for other services (AI machines' keys, plug-ins'
# secret settings): keep a copy with your backups, or those are entered again.
LUMIVERB_SECRETS_KEY=${LUMIVERB_SECRETS_KEY}

# Storage
STORAGE_PROVIDER=local
DATA_DIR=${DATA_DIR}
XDG_CACHE_HOME=${DATA_DIR}/cache

# API
API_LISTEN_HOST=${API_LISTEN_HOST}
API_PORT=${API_PORT}

# What this install uses (remembered for reruns)
PG_PORT=${PG_PORT}
PG_VERSION=${PG_VERSION}
BRANCH=${BRANCH}
NO_FIREWALL=${NO_FIREWALL}

# App
APP_ENV=production
APP_HOST=${APP_HOST}
LOG_LEVEL=INFO
${EXPOSURE:+
# From anywhere but this machine, only the routes this names answer (src/server/api/exposure.py)
LUMIVERB_EXPOSURE=${EXPOSURE}
}
# Password reset SMTP (uncomment and fill in to enable forgot-password)
# SMTP_HOST=smtp.example.com
# SMTP_PORT=587
# SMTP_USER=apikey
# SMTP_PASSWORD=
# SMTP_FROM=noreply@${DOMAIN:-lumiverb.local}
ENVEOF
# What earlier versions wrote for Quickwit, which nothing reads now (ADR-019),
# isn't kept as if it had been added by hand.
KEPT="$(awk -F= 'NR == FNR { if (/^[A-Za-z_][A-Za-z0-9_]*=/) ours[$1] = 1; next }
  !($1 in ours) && $1 !~ /^QUICKWIT_(URL|ENABLED|PORT)$/' "$ENV_FILE" - <<< "$EARLIER")"
[[ -z "$KEPT" ]] || printf '\n# Added by hand, kept by deploy-api.sh\n%s\n' "$KEPT" >> "$ENV_FILE"

chmod 600 "$ENV_FILE"
ok "Secrets generated, env written (API on ${API_LISTEN_HOST}:${API_PORT}, Postgres on ${PG_PORT})"

# ---------------------------------------------------------------------------
# 6. Clone / update application
# ---------------------------------------------------------------------------
step "Deploying application to ${APP_DIR}"

if [[ -d "${APP_DIR}/.git" ]]; then
  # The checkout belongs to $SVC_USER, and git runs in it only as that user: its own settings (.git/config: hooks,
  # an fsmonitor) never steer a git of root's. Earlier versions marked it safe for root (safe.directory): unmarked.
  git config --system --fixed-value --unset-all safe.directory "$APP_DIR" 2>/dev/null || true
  git config --global --fixed-value --unset-all safe.directory "$APP_DIR" 2>/dev/null || true
  chown -R "$SVC_USER":"$SVC_USER" "$APP_DIR"  # what root's git made there before, the service user's
  cd "$APP_DIR"
  as_svc git fetch --all --prune
  as_svc git checkout "$BRANCH"
  as_svc git pull origin "$BRANCH"
  ok "Updated existing checkout"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  ok "Cloned ${REPO_URL} @ ${BRANCH}"
fi

cd "$APP_DIR"
chown -R "$SVC_USER":"$SVC_USER" "$APP_DIR"

# ---------------------------------------------------------------------------
# 7. Python dependencies
# ---------------------------------------------------------------------------
step "Installing Python dependencies"

EXTRAS=(--extra cli --extra embeddings --extra face_recognition)
[[ "$WITH_WORKER" != "true" ]] || EXTRAS+=(--extra workers)
as_svc "$UV_BIN" sync "${EXTRAS[@]}"
ok "Python venv ready (${EXTRAS[*]})"

# ---------------------------------------------------------------------------
# 8. Run migrations
# ---------------------------------------------------------------------------
step "Running database migrations"

# The scheduler stops first, as in update-api.sh: a migration may drop what
# its old code reads. "Starting the scheduler" starts it again.
if systemctl is-enabled lumiverb-scheduler >/dev/null 2>&1; then
  systemctl stop lumiverb-scheduler
  ok "Scheduler stopped for the migrations"
fi

export ALEMBIC_CONTROL_URL="${DB_URL}/${PG_DB}"
sudo -u "$SVC_USER" --preserve-env=ALEMBIC_CONTROL_URL \
  "$APP_DIR/.venv/bin/python" -m alembic -c alembic-control.ini upgrade head
ok "Control plane migrations applied"

# Lumiverb's own plug-ins, as migrate.sh's last step installs them (src/server/own_plugins.py), into
# DATA_DIR/plugins (made here, as the service user). One that can't be is said and tried again at the next start.
export CONTROL_PLANE_DATABASE_URL="${DB_URL}/${PG_DB}" DATA_DIR
LUMIVERB_GEONAMES_URL="$(_existing_val LUMIVERB_GEONAMES_URL)"
export LUMIVERB_GEONAMES_URL
if sudo -u "$SVC_USER" --preserve-env=CONTROL_PLANE_DATABASE_URL,DATA_DIR,LUMIVERB_GEONAMES_URL \
    "$APP_DIR/.venv/bin/python" -m src.server.own_plugins; then
  ok "Lumiverb's own plug-ins looked at"
else
  warn "Lumiverb's own plug-ins weren't looked at; the plug-in runner tries at its start"
fi

# ---------------------------------------------------------------------------
# 9. systemd units
# ---------------------------------------------------------------------------
step "Installing systemd units"

cat > /etc/systemd/system/lumiverb-api.service <<UNIT
[Unit]
Description=Lumiverb API Server
After=network.target postgresql.service

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
# Large uploads (analysis proxies) are spooled to TMPDIR; PrivateTmp's /tmp
# can be RAM (tmpfs), so they go on the data disk.
Environment=TMPDIR=${DATA_DIR}/tmp
# PrivateTmp emptied /tmp on each start; do the same for uploads a killed API left.
ExecStartPre=-/usr/bin/find ${DATA_DIR}/tmp -mindepth 1 -delete
# uvicorn, through python -m src.server.api: none of its processes, which hold this file's secrets, is dumpable,
# so the other processes of its user, a plug-in's among them, can't read their environment or memory.
ExecStart=${APP_DIR}/.venv/bin/python -m src.server.api src.server.api.main:app --host \${API_LISTEN_HOST} --port \${API_PORT} --workers 2
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

# All processing (ADR-016 phase 4): scans, previews, analysis copies, the
# AI, in one ranked queue. Everything outside DATA_DIR is read-only to it,
# storage mounts and its home included (nothing it runs writes there,
# matplotlib's settings and CUDA's kernels included: both are on the data disk).
cat > /etc/systemd/system/lumiverb-scheduler.service <<UNIT
[Unit]
Description=Lumiverb scheduler (all processing)
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
# CUDA's compiled kernels too, where a GPU needs them made (its home is read-only).
Environment=CUDA_CACHE_PATH=${DATA_DIR}/cache/nvidia
# Temp files too (Whisper's WAVs): PrivateTmp's /tmp can be RAM. Not the
# API's tmp, which each API start empties; this one each scheduler start.
Environment=TMPDIR=${DATA_DIR}/worker-tmp
ExecStartPre=-/usr/bin/find ${DATA_DIR}/worker-tmp -mindepth 1 -delete
ExecStart=${APP_DIR}/.venv/bin/python -m src.server.scheduler
Restart=on-failure
RestartSec=30s
# It lets jobs in hand finish for up to 25 s, saving nothing more; only the
# scheduler hears the stop, so ffmpeg under a job isn't killed mid-clip
# (its output read as "no audio"). What's left is killed at the end.
KillMode=mixed
TimeoutStopSec=40s
LimitNOFILE=65535
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${DATA_DIR}

[Install]
WantedBy=multi-user.target
UNIT

# Every plug-in the server admin installs (ADR-023), a process of its own:
# the runner reads what's installed from the control database. It keeps only
# the env file's database addresses and sealing key, and isn't dumpable
# (src/shared/secrets_env.py); what it starts gets none of the env file's
# settings and runs below the API and the scheduler. Each plug-in runs as the
# service user: it can read what that user can (DATA_DIR's previews and
# backups among them) and write DATA_DIR/plugins, every plug-in's folder
# there (ADR-023's phase 4, confining it, is deferred).
svc_folder "$DATA_DIR"/plugins
cat > /etc/systemd/system/lumiverb-plugins.service <<UNIT
[Unit]
Description=Lumiverb plug-ins (each installed plug-in, a process of its own)
After=network-online.target lumiverb-api.service
Wants=network-online.target

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
Environment=HOME=${SVC_HOME}
ExecStart=${APP_DIR}/.venv/bin/python -m src.server.plugin_runner
Restart=always
RestartSec=10s
# Only the runner hears the stop: it stops its plug-ins within 10 s; what's
# left is killed at the end.
KillMode=mixed
TimeoutStopSec=20s
# Every plug-in at once, at most (each is held to its own memory_mb by the runner).
MemoryMax=4G
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=${DATA_DIR}/plugins

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
# The install's key reaches curl on its stdin (-H @-), never as an argument: every local user sees command lines.
ExecStart=/bin/sh -c 'printf "Authorization: Bearer %%s\n" "\$\$ADMIN_KEY" | exec /usr/bin/curl -sf --retry 12 --retry-delay 5 --retry-connrefused -X POST "http://127.0.0.1:\$\${API_PORT}/v1/upkeep?tenants=all" -H @- -H "Content-Type: application/json"'
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
# The install's key reaches curl on its stdin (-H @-), never as an argument: every local user sees command lines.
ExecStart=/bin/sh -c 'printf "Authorization: Bearer %%s\n" "\$\$ADMIN_KEY" | exec /usr/bin/curl -sf --retry 12 --retry-delay 5 --retry-connrefused -X POST "http://127.0.0.1:\$\${API_PORT}/v1/upkeep/cleanup?tenants=all&dry_run=false" -H @- -H "Content-Type: application/json"'
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
# Search is PostgreSQL's own (ADR-019): an earlier install's Quickwit is
# unused, and the update takes it away.
if [[ -f /etc/systemd/system/lumiverb-quickwit.service ]]; then
  warn "Quickwit from an earlier install is still here, unused: sudo bash ${APP_DIR}/scripts/update.sh removes it"
fi

# ---------------------------------------------------------------------------
# 10. Firewall
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
# 11. Start services
# ---------------------------------------------------------------------------
step "Starting services"

systemctl enable --now lumiverb-api lumiverb-plugins lumiverb-upkeep.timer lumiverb-upkeep-daily.timer

# Its first start compiles the code (no .pyc yet): up to 90 s, stopping at once if the unit fails.
for i in {1..90}; do
  if curl -sf http://127.0.0.1:${API_PORT}/health >/dev/null 2>&1; then
    break
  fi
  systemctl is-failed --quiet lumiverb-api && break
  (( i % 10 == 0 )) && echo "  Waiting for the API to start (${i} s)..."
  sleep 1
done

if curl -sf http://127.0.0.1:${API_PORT}/health >/dev/null 2>&1; then
  ok "API server healthy"
else
  warn "API server not responding yet — check: journalctl -u lumiverb-api -n 50"
fi

systemctl status --no-pager lumiverb-api || true

# ---------------------------------------------------------------------------
# 12. Provision default tenant (first run only)
# ---------------------------------------------------------------------------
# The service user's own file (CLI_CONFIG), read and written only as that user: root opens nothing in its home.
if account_made; then
  step "Tenant"
  ok "Already provisioned (${CLI_CONFIG} has a key); not creating another"
  # Keep the service user's CLI pointed at this install's API port (-I: no Python of that user's own on the way).
  sudo -u "${SVC_USER}" -H python3 -I - "$CLI_CONFIG" "$API_PORT" <<'PY'
import json, sys
path, port = sys.argv[1], sys.argv[2]
cfg = json.load(open(path))
cfg["api_url"] = f"http://127.0.0.1:{port}"
json.dump(cfg, open(path, "w"), indent=2)
PY
else
step "Provisioning tenant: ${TENANT_NAME}"

ADMIN_KEY="$(grep -m1 '^ADMIN_KEY=' "${ENV_FILE}" | cut -d= -f2-)"
# JSON made by Python, each value only what it is whatever it holds, handed over in its environment (the vision key
# never an argument).
TENANT_BODY="$(BODY_NAME="$TENANT_NAME" BODY_EMAIL="$CERTBOT_EMAIL" BODY_VISION_URL="$VISION_API_URL" \
  BODY_VISION_KEY="$VISION_API_KEY" python3 -I -c '
import json, os
body = {"name": os.environ["BODY_NAME"], "email": os.environ["BODY_EMAIL"]}
if os.environ["BODY_VISION_URL"]:
    body["vision_api_url"] = os.environ["BODY_VISION_URL"]
if os.environ["BODY_VISION_KEY"]:
    body["vision_api_key"] = os.environ["BODY_VISION_KEY"]
print(json.dumps(body))')"

# The install's key reaches curl through a pipe, and the body (perhaps a vision key) on its stdin: neither is ever
# an argument, which every local user can see.
TENANT_RESPONSE="$(printf '%s' "$TENANT_BODY" | curl -sf -X POST "http://127.0.0.1:${API_PORT}/v1/admin/tenants" \
  -H @<(printf 'Authorization: Bearer %s\n' "$ADMIN_KEY") \
  -H "Content-Type: application/json" \
  --data-binary @-)" \
  || fail "Tenant provisioning failed — check: journalctl -u lumiverb-api -n 50"

TENANT_ID="$(echo "$TENANT_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['tenant_id'])")"
TENANT_API_KEY="$(echo "$TENANT_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])")"
ok "Tenant ${TENANT_ID} provisioned"

sudo -u "${SVC_USER}" mkdir -p "${SVC_HOME}/.lumiverb"
# Readable by the service user alone (umask) from the start: it holds the account's key.
echo "{\"api_url\": \"http://127.0.0.1:${API_PORT}\", \"api_key\": \"${TENANT_API_KEY}\"}" \
  | (umask 077 && sudo -u "${SVC_USER}" tee "$CLI_CONFIG" > /dev/null)
sudo -u "${SVC_USER}" chmod 600 "$CLI_CONFIG"
ok "CLI configured for tenant"
fi

# ---------------------------------------------------------------------------
# 13. Scheduler: where library roots are on this machine, then start it
# ---------------------------------------------------------------------------
# The scheduler's own settings (LUMIVERB_* in the env file; never the CLI's
# config): what an earlier install put in the service user's CLI config is
# carried over once, then each --root-map. The CLI maps them too, so
# `lumiverb library list` as the service user shows where each library is.
# The CLI config is read as the service user and handed over on stdin; only a single line of the type each
# setting takes is carried.
ROOT_MAP_ARGS=()
for map in "${ROOT_MAPS[@]}"; do ROOT_MAP_ARGS+=(--root-map "$map"); done
{ as_svc timeout 10 head -c 1048576 -- "$CLI_CONFIG" 2>/dev/null || true; } \
  | python3 "${APP_DIR}/scripts/scheduler-env.py" "$ENV_FILE" --from-cli-config - "${ROOT_MAP_ARGS[@]}"
for map in "${ROOT_MAPS[@]}"; do
  sudo -u "${SVC_USER}" -H "${APP_DIR}/.venv/bin/lumiverb" config map-root "${map%%=*}" "${map#*=}"
done
if [[ "$WITH_WORKER" == "true" ]]; then
  step "Starting the scheduler"
  # It replaces the worker: the two never run together. The worker's stop
  # reaches only its main process (as in update-api.sh).
  if [[ -f /etc/systemd/system/lumiverb-worker.service ]]; then
    mkdir -p /etc/systemd/system/lumiverb-worker.service.d
    printf '[Service]\nKillMode=mixed\n' > /etc/systemd/system/lumiverb-worker.service.d/stop.conf
    systemctl daemon-reload
    systemctl disable --now lumiverb-worker 2>/dev/null || true
    rm -rf /etc/systemd/system/lumiverb-worker.service /etc/systemd/system/lumiverb-worker.service.d
    systemctl daemon-reload
  fi
  systemctl enable lumiverb-scheduler
  systemctl restart lumiverb-scheduler
  ok "lumiverb-scheduler running (journalctl -u lumiverb-scheduler -f)"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}Lumiverb API deployed.${NC}"
echo ""
echo "API listening on ${API_LISTEN_HOST}:${API_PORT}; people reach it at ${APP_HOST}"
echo ""
[[ -z "$LOW_MEMORY" ]] || { warn "$LOW_MEMORY"; echo ""; }
echo "Next steps:"
echo ""
echo "  1. Create the first admin (while nobody can log in), here in this session: no screen needed"
EXAMPLE_EMAIL="${CERTBOT_EMAIL:-you@example.com}"
echo "     sudo /opt/lumiverb/.venv/bin/lumiverb-server setup --env ${ENV_FILE} --email ${EXAMPLE_EMAIL}"
if [[ -n "$EXPOSURE" ]]; then
  echo "     From anywhere but this machine, only what LUMIVERB_EXPOSURE=${EXPOSURE} lists answers, signing in"
  echo "     with a password included: sign in through an SSH tunnel (docs/demo-server.md)."
fi
echo ""
echo "  2. Deploy the web UI (same or different machine):"
echo "     bash scripts/deploy-web.sh --domain app.lumiverb.io --api-upstream http://${API_LISTEN_HOST}:${API_PORT}"
echo ""
echo "Useful commands:"
echo "  journalctl -u lumiverb-api -f          # API logs"
echo "  journalctl -u lumiverb-scheduler -f    # Scheduler logs (all processing)"
echo "  systemctl restart lumiverb-api          # Restart API"
echo ""
echo "Config: ${ENV_FILE} (contains secrets — use 'sudo cat' with care)"
echo ""
