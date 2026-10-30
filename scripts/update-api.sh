#!/usr/bin/env bash
# Update the Lumiverb API server on an existing deployment.
#
# Usage (from VPS):
#   bash /opt/lumiverb/scripts/update-api.sh
#
# Or remote:
#   ssh root@your-vps 'bash /opt/lumiverb/scripts/update-api.sh'
#
# What it does:
#   1. git pull
#   2. uv sync (if Python changes: built beside the running one, then a short stop to swap)
#   3. Run migrations (control plane + tenants)
#   4. Sync data directory
#   5. Remove Quickwit, if the install has it (search is in PostgreSQL: ADR-019)
#   6. Install upkeep timers
#   7. Restart API + scheduler
#   8. Health check
#
# Does NOT: rebuild web UI, touch nginx, install Node.js.
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
CONF_DIR="/etc/lumiverb"
ENV_FILE="${CONF_DIR}/env"
SVC_USER="lumiverb"
UV_BIN="/usr/local/bin/uv"

# Adds a line to the env file, on a line of its own even when the file
# doesn't end in a newline.
env_append() {
  if [[ -s "$ENV_FILE" && -n "$(tail -c1 "$ENV_FILE")" ]]; then
    echo >> "$ENV_FILE"
  fi
  echo "$1" >> "$ENV_FILE"
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
read_data_dir() {
  local count
  count="$(grep -c '^DATA_DIR=' "$ENV_FILE" || true)"
  [[ "${count:-0}" -le 1 ]] || fail "DATA_DIR is set ${count} times in ${ENV_FILE}: keep one line"
  DATA_DIR="$(grep -m1 '^DATA_DIR=' "$ENV_FILE" | cut -d= -f2- || true)"
  data_dir_shape "$DATA_DIR"
}

[[ "$(id -u)" -eq 0 ]] || fail "Run as root"
[[ -d "${APP_DIR}/.git" ]] || fail "${APP_DIR} is not a git repo — run deploy-api.sh first"

# Ensure the service user's HOME exists.
SVC_HOME="$(getent passwd "$SVC_USER" | cut -d: -f6)"
if [[ -n "$SVC_HOME" ]] && [[ ! -d "$SVC_HOME" ]]; then
  mkdir -p "$SVC_HOME"
  chown -h "$SVC_USER":"$SVC_USER" "$SVC_HOME"
fi

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
  exec bash "$APP_DIR/scripts/update-api.sh" "$@"
fi

# ---------------------------------------------------------------------------
step "Updating Python dependencies"
EXTRAS=(--extra cli --extra embeddings --extra face_recognition)
# Before anything changes: a DATA_DIR that isn't one full path stops here.
read_data_dir
# uv sync removes what the extras don't list, so keep processing's (the
# scheduler's, or the worker's it replaces).
PROCESSING=false
if systemctl is-enabled lumiverb-scheduler >/dev/null 2>&1 || systemctl is-enabled lumiverb-worker >/dev/null 2>&1; then
  PROCESSING=true
  EXTRAS+=(--extra workers)
  # Before anything changes too: the scheduler's caches need the data disk.
  [[ -n "$DATA_DIR" ]] || fail "No DATA_DIR in ${ENV_FILE}: the scheduler's caches need the data disk"
fi
# A new Python makes uv sync delete the venv and download it all again. So
# download first, into a side venv, while the API and scheduler run; then stop
# them only for the swap, which the warm cache makes quick. Both files are the
# service user's, read as it.
HAVE_PY="$(as_svc sed -n 's/^version_info *= *\([0-9]*\.[0-9]*\).*/\1/p' "$APP_DIR/.venv/pyvenv.cfg" 2>/dev/null || true)"
WANT_PY="$(as_svc grep -m1 -oE '[0-9]+\.[0-9]+' "$APP_DIR/.python-version" 2>/dev/null | head -1 || true)"
# From here a failure may leave Lumiverb stopped (now, or by an earlier run): say so.
# The scheduler isn't started for you: the code may be half-deployed.
stopped_after_failure() {
  local rc=$?
  [[ $rc -ne 0 ]] || return 0
  if ! systemctl is-active --quiet lumiverb-api; then
    echo -e "${RED}  ✗ Lumiverb isn't running. Fix the error above, then run the update again (update.sh): it carries on from here.${NC}" >&2
  fi
  if [[ -z "${LUMIVERB_UPDATE_LOG:-}" ]] && scheduler_stopped; then
    echo -e "${RED}  ✗ $(scheduler_stopped_line)${NC}" >&2
  fi
}
scheduler_stopped() {
  systemctl is-enabled --quiet lumiverb-scheduler 2>/dev/null && ! systemctl is-active --quiet lumiverb-scheduler
}
scheduler_stopped_line() {
  echo "The scheduler is stopped. Run the update again; to start it as it is: sudo systemctl start lumiverb-scheduler"
}
trap stopped_after_failure EXIT
if [[ -n "$HAVE_PY" && -n "$WANT_PY" && "$HAVE_PY" != "$WANT_PY" ]]; then
  warn "Python changes from ${HAVE_PY} to ${WANT_PY}: building the new environment (a few GB) while Lumiverb keeps running"
  as_svc "$UV_BIN" python install "$WANT_PY"
  rm -rf "$APP_DIR/.venv-next"  # uv won't build into what an interrupted run left
  as_svc UV_PROJECT_ENVIRONMENT="$APP_DIR/.venv-next" "$UV_BIN" sync "${EXTRAS[@]}"
  # The old worker's stop reaches only its main process (as when the scheduler
  # replaces it below): killed with its ffmpeg, it would save an empty transcript.
  if [[ -f /etc/systemd/system/lumiverb-worker.service ]]; then
    mkdir -p /etc/systemd/system/lumiverb-worker.service.d
    printf '[Service]\nKillMode=mixed\n' > /etc/systemd/system/lumiverb-worker.service.d/stop.conf
    systemctl daemon-reload
  fi
  for unit in lumiverb-scheduler lumiverb-worker; do
    systemctl is-enabled "$unit" >/dev/null 2>&1 && systemctl stop "$unit"
  done
  systemctl stop lumiverb-api
fi
as_svc "$UV_BIN" sync "${EXTRAS[@]}"
rm -rf "$APP_DIR/.venv-next"
ok "Python venv synced (${EXTRAS[*]})"

# ---------------------------------------------------------------------------
step "Running migrations"
# The scheduler stops first: a migration may drop what its old code reads
# (it would count crashes against clips while the API answers 500s). It
# starts again last, once the API answers.
if systemctl is-enabled lumiverb-scheduler >/dev/null 2>&1; then
  systemctl stop lumiverb-scheduler
  ok "Scheduler stopped for the migrations"
fi
# The plug-in runner too: it reads what's installed from the control database.
if systemctl is-enabled lumiverb-plugins >/dev/null 2>&1; then
  systemctl stop lumiverb-plugins
  ok "Plug-in runner stopped for the migrations"
fi

DB_URL="$(grep '^CONTROL_PLANE_DATABASE_URL=' "$ENV_FILE" | cut -d= -f2-)"
[[ -n "$DB_URL" ]] || fail "CONTROL_PLANE_DATABASE_URL not found in ${ENV_FILE}"

# A backup before this version's migrations, as a Mac's service makes one
# (src/server/backups.py: the 3 newest kept; into the backups folder, else
# DATA_DIR/backups), only when there's something to migrate. One that can't
# be made is warned of and the update goes on: the nightly ones are there.
read_data_dir
if CONTROL_PLANE_DATABASE_URL="$DB_URL" DATA_DIR="$DATA_DIR" \
    sudo -u "$SVC_USER" --preserve-env=CONTROL_PLANE_DATABASE_URL,DATA_DIR \
    "$APP_DIR/.venv/bin/lumiverb-server" backup --if-migrating; then
  ok "Backed up before migrating (when there was anything to migrate)"
else
  warn "No backup before migrating (above); migrating anyway: the nightly backups are there"
fi

export ALEMBIC_CONTROL_URL="$DB_URL"
sudo -u "$SVC_USER" --preserve-env=ALEMBIC_CONTROL_URL \
  "$APP_DIR/.venv/bin/python" -m alembic -c alembic-control.ini upgrade head
ok "Control plane migrations applied"

export CONTROL_PLANE_DATABASE_URL="$DB_URL"
export UV_BIN
# Its last step installs Lumiverb's own plug-ins (src/server/own_plugins.py) into DATA_DIR/plugins, making it.
# Its uv reads none of the service user's settings (SVC_ENV).
read_data_dir
LUMIVERB_GEONAMES_URL="$(grep -m1 '^LUMIVERB_GEONAMES_URL=' "$ENV_FILE" | cut -d= -f2- || true)"
export DATA_DIR LUMIVERB_GEONAMES_URL
sudo -u "$SVC_USER" --preserve-env=CONTROL_PLANE_DATABASE_URL,ALEMBIC_CONTROL_URL,UV_BIN,DATA_DIR,LUMIVERB_GEONAMES_URL \
  "${SVC_ENV[@]}" bash "$APP_DIR/scripts/migrate.sh"
ok "Tenant migrations applied"

# ---------------------------------------------------------------------------
step "Scheduler settings"
# The scheduler reads its own settings (LUMIVERB_* in this env file), never
# the CLI's config. What it read from the service user's CLI config before
# (library roots, how video is rendered) is carried over once; a setting
# already here stays. Before anything saves that config (config set, below),
# which keeps only the CLI's own keys. The config is the service user's, read
# as it (root opens nothing in its home) and handed over on stdin; only a
# single line of the type each setting takes is carried.
if [[ "$PROCESSING" == "true" ]]; then
  { as_svc timeout 10 head -c 1048576 -- "${SVC_HOME}/.lumiverb/config.json" 2>/dev/null || true; } \
    | python3 "$APP_DIR/scripts/scheduler-env.py" "$ENV_FILE" --from-cli-config -
  grep -q '^LUMIVERB_ROOT_MAP=' "$ENV_FILE" \
    || warn "No LUMIVERB_ROOT_MAP in ${ENV_FILE}: libraries are read at the paths the server stores"
  ok "Scheduler settings in ${ENV_FILE}"
else
  ok "No processing on this machine"
fi

# ---------------------------------------------------------------------------
step "Ensuring data directory"
read_data_dir
if [[ -n "$DATA_DIR" ]]; then
  svc_folder "$DATA_DIR"/tmp "$DATA_DIR"/worker-tmp
  # -R changes links themselves, never what they point at, and never goes through one.
  chown -R "$SVC_USER":"$SVC_USER" "$DATA_DIR"
  # The scheduler's caches, lock and state on the data disk. The CLI keeps
  # no caches (ADR-020 decision 7), so nothing of the service user's points here.
  grep -q '^XDG_CACHE_HOME=' "$ENV_FILE" || env_append "XDG_CACHE_HOME=${DATA_DIR}/cache"
  ok "Data dir: $DATA_DIR"
fi

# ---------------------------------------------------------------------------
# Search is PostgreSQL's own (ADR-019): an install that had Quickwit loses it,
# its index too (derived data, already rebuilt in each account's database).
# Only what deploy-api.sh made for it: its unit, its config, its folder in
# DATA_DIR (nothing else there) and the binary it put in /usr/local/bin.
# Nothing to do, nothing said: an install without it, or the next update.
QW_UNIT="/etc/systemd/system/lumiverb-quickwit.service"
QW_CONF="${CONF_DIR}/quickwit.yaml"
QW_DATA="${DATA_DIR:+${DATA_DIR}/quickwit}"
QW_BIN="/usr/local/bin/quickwit"
if [[ -e "$QW_UNIT" || -e "$QW_CONF" || ( -n "$QW_DATA" && ( -e "$QW_DATA" || -L "$QW_DATA" ) ) \
      || -e "$QW_BIN" || -L "$QW_BIN" ]]; then
  step "Removing Quickwit (search is in PostgreSQL now)"
  if [[ -e "$QW_UNIT" ]]; then
    systemctl disable --now lumiverb-quickwit 2>/dev/null || true
    rm -rf "$QW_UNIT" "${QW_UNIT}.d"
    systemctl daemon-reload
    ok "lumiverb-quickwit stopped and removed"
  fi
  if [[ -e "$QW_CONF" ]]; then
    rm -f "$QW_CONF"
    ok "Removed ${QW_CONF}"
  fi
  if [[ -n "$QW_DATA" && ( -e "$QW_DATA" || -L "$QW_DATA" ) ]]; then
    rm -rf -- "$QW_DATA"
    ok "Removed ${QW_DATA} (its index)"
  fi
  if [[ -e "$QW_BIN" || -L "$QW_BIN" ]]; then
    rm -f "$QW_BIN"
    ok "Removed ${QW_BIN}"
  fi
  # The env file is the admin's: its Quickwit settings stay, said once.
  QW_KEYS="$(grep -oE '^QUICKWIT_[A-Za-z0-9_]*' "$ENV_FILE" 2>/dev/null | paste -sd ' ' - || true)"
  if [[ -n "$QW_KEYS" ]]; then
    ok "${QW_KEYS// /, } in ${ENV_FILE}: no longer used, they may be removed"
  fi
fi

# ---------------------------------------------------------------------------
step "Secrets key"
# What seals the secrets Lumiverb keeps for other services (src/server/sealing.py): an install made before it
# gets one, a Fernet key (32 random bytes, URL-safe base64). Keep a copy with your backups.
grep -q '^LUMIVERB_SECRETS_KEY=' "$ENV_FILE" \
  || env_append "LUMIVERB_SECRETS_KEY=$(openssl rand -base64 32 | tr '+/' '-_')"
ok "LUMIVERB_SECRETS_KEY in ${ENV_FILE}"

# ---------------------------------------------------------------------------
step "Ports"
# Installs from before ports were configurable run the API on 8000.
grep -q '^API_PORT=' "$ENV_FILE" || env_append "API_PORT=8000"
API_PORT="$(grep '^API_PORT=' "$ENV_FILE" | cut -d= -f2-)"
ok "API on port ${API_PORT}"

# ---------------------------------------------------------------------------
step "Updating the API unit"
API_UNIT="/etc/systemd/system/lumiverb-api.service"
if [[ -f "$API_UNIT" ]]; then
  # Quickwit's unit is gone (above): the API no longer waits for it or wants it.
  if grep -qE '^(After|Wants|Requires)=.*lumiverb-quickwit\.service' "$API_UNIT"; then
    sed -i -E '/^(After|Wants|Requires)=/ {
        s/(=|[[:space:]])lumiverb-quickwit\.service([[:space:]]|$)/\1\2/g
        s/=[[:space:]]+/=/; s/[[:space:]]+$//; s/[[:space:]]{2,}/ /g
      }
      /^(After|Wants|Requires)=$/d' "$API_UNIT"
    systemctl daemon-reload
    ok "The API no longer waits for Quickwit"
  fi
  # Large uploads (analysis proxies) are spooled to TMPDIR; PrivateTmp's /tmp
  # can be RAM (tmpfs), so they go on the data disk (inside ReadWritePaths).
  if [[ -z "$DATA_DIR" ]]; then
    warn "No DATA_DIR in ${ENV_FILE}; API uploads stay in /tmp"
  else
    if ! grep -q "^Environment=TMPDIR=" "$API_UNIT"; then
      sed -i "/^Environment=PYTHONUNBUFFERED=1$/a Environment=TMPDIR=${DATA_DIR}/tmp" "$API_UNIT"
      ok "API uploads spool to ${DATA_DIR}/tmp"
    fi
    # PrivateTmp emptied /tmp on each start; do the same for uploads a killed API left.
    if ! grep -q "^ExecStartPre=-/usr/bin/find ${DATA_DIR}/tmp " "$API_UNIT"; then
      sed -i "/^Environment=TMPDIR=/a ExecStartPre=-/usr/bin/find ${DATA_DIR}/tmp -mindepth 1 -delete" "$API_UNIT"
    fi
    systemctl daemon-reload
  fi
  # uvicorn's own process holds the env file's secrets and starts the workers: through python -m src.server.api
  # it isn't dumpable, so the other processes of its user, a plug-in's among them, can't read them (as deploy-api.sh
  # writes it now; src/shared/secrets_env.py).
  UVICORN="^ExecStart=${APP_DIR}/\.venv/bin/uvicorn (src\.server\.api\.main:app( |$))"
  if grep -qE "$UVICORN" "$API_UNIT"; then
    sed -i -E "s#${UVICORN}#ExecStart=${APP_DIR}/.venv/bin/python -m src.server.api \1#" "$API_UNIT"
    systemctl daemon-reload
    ok "The API starts through python -m src.server.api: none of its processes is dumpable"
  elif ! grep -qE "^ExecStart=.* -m src\.server\.api " "$API_UNIT"; then
    warn "${API_UNIT}'s ExecStart isn't deploy-api.sh's: uvicorn's own process stays readable to the other processes of its user until it runs ${APP_DIR}/.venv/bin/python -m src.server.api with uvicorn's arguments"
  fi
fi

# ---------------------------------------------------------------------------
step "Installing the scheduler (all processing; it replaces the worker)"
if [[ "$PROCESSING" == "true" ]]; then
  read_data_dir
  [[ -n "$DATA_DIR" ]] || fail "No DATA_DIR in ${ENV_FILE}: the scheduler's caches need the data disk"
  svc_folder "$DATA_DIR"/worker-tmp "$DATA_DIR"/cache
  # Rewritten each update: everything it needs is known here. It writes only
  # the data disk: its home is read-only to it (nothing it runs writes there,
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
  # The worker goes: the scheduler takes its lock, state and caches. Its stop
  # reaches only its main process (the rest dies with it at the end): its old
  # code, killed together with the ffmpeg under a transcription, would save
  # an empty transcript.
  if [[ -f /etc/systemd/system/lumiverb-worker.service ]]; then
    mkdir -p /etc/systemd/system/lumiverb-worker.service.d
    printf '[Service]\nKillMode=mixed\n' > /etc/systemd/system/lumiverb-worker.service.d/stop.conf
    systemctl daemon-reload
    systemctl disable --now lumiverb-worker 2>/dev/null || true
    rm -rf /etc/systemd/system/lumiverb-worker.service /etc/systemd/system/lumiverb-worker.service.d
    ok "lumiverb-worker stopped and removed"
  fi
  systemctl daemon-reload
  systemctl enable lumiverb-scheduler >/dev/null 2>&1
  ok "lumiverb-scheduler installed"
else
  ok "No processing on this machine (install with deploy-api.sh --worker)"
fi

# ---------------------------------------------------------------------------
step "Photo decoders"
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
# Photos are read with libvips 8.13 or later, the first that can turn off its untrusted loaders:
# src/processing/libvips.py refuses an older one (TRUSTED_ONLY_SINCE), and scans stop, saying why. Ubuntu 22.04's
# is 8.12, 24.04's 8.15. Held to the same text in deploy-api.sh and update-api.sh.
LIBVIPS_NEEDED=8.13
libvips_too_old() {
  [[ -n "$1" ]] && dpkg --compare-versions "$1" lt "$LIBVIPS_NEEDED"
}
# The libvips installed here: one too old is said, and the update goes on (all but scans works as before, and the
# untrusted loaders are never used again).
libvips_check() {
  local vips
  vips="$(dpkg-query -W -f='${db:Status-Abbrev}${Version}\n' libvips42t64 libvips42 2>/dev/null \
    | awk '$1 == "ii" { print $2; exit }' || true)"
  if libvips_too_old "$vips"; then
    warn "This system's libvips is ${vips%%-*}: Lumiverb reads photos with libvips ${LIBVIPS_NEEDED} or later, the first that can turn off its untrusted loaders, so scans stop until it's newer (Settings › Processing says why). Ubuntu 24.04 or later has one. Everything else works as before."
  elif [[ -n "$vips" ]]; then
    ok "libvips ${vips%%-*}"
  fi
}
if [[ "$PROCESSING" == "true" ]]; then
  libvips_check
  heic_decoder
else
  ok "No processing here: nothing to decode"
fi

# ---------------------------------------------------------------------------
step "Installing the plug-in runner"
read_data_dir
if [[ -n "$DATA_DIR" ]]; then
  svc_folder "$DATA_DIR"/plugins
  # Every plug-in the server admin installs (ADR-023), a process of its own:
  # the runner reads what's installed from the control database. It keeps only
  # the env file's database addresses and sealing key, and isn't dumpable
  # (src/shared/secrets_env.py); what it starts gets none of the env file's
  # settings and runs below the API and the scheduler. Each plug-in runs as the
  # service user: it can read what that user can (DATA_DIR's previews and
  # backups among them) and write DATA_DIR/plugins, every plug-in's folder
  # there (ADR-023's phase 4, confining it, is deferred).
  # Rewritten each update: everything it needs is known here.
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
  systemctl daemon-reload
  systemctl enable lumiverb-plugins >/dev/null 2>&1
  ok "lumiverb-plugins installed"
else
  warn "No DATA_DIR in ${ENV_FILE}: no plug-in runner (plug-ins keep their folders on the data disk)"
  warn "Place names (GeoNames) is a plug-in: without DATA_DIR there are no place names or Place search"
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
# The install's key reaches curl on its stdin (-H @-), never as an argument: every local user sees command lines.
ExecStart=/bin/sh -c 'printf "Authorization: Bearer %%s\n" "\$\$ADMIN_KEY" | exec /usr/bin/curl -sf --retry 12 --retry-delay 5 --retry-connrefused -X POST "http://127.0.0.1:\$\${API_PORT}/v1/upkeep?tenants=all" -H @- -H "Content-Type: application/json"'
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
# The install's key reaches curl on its stdin (-H @-), never as an argument: every local user sees command lines.
ExecStart=/bin/sh -c 'printf "Authorization: Bearer %%s\n" "\$\$ADMIN_KEY" | exec /usr/bin/curl -sf --retry 12 --retry-delay 5 --retry-connrefused -X POST "http://127.0.0.1:\$\${API_PORT}/v1/upkeep/cleanup?tenants=all&dry_run=false" -H @- -H "Content-Type: application/json"'
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
step "Giving the GPU back to containers"
# systemd reloading its units (daemon-reload, above) takes the GPU from
# running containers that use it (nvidia-container-toolkit with systemd's
# cgroups): prod's Ollama then runs its model on the CPU, with no error,
# until it's restarted (Oct 9). One that can't say (no nvidia-smi) is left alone.
if command -v docker >/dev/null 2>&1; then
  for c in $(docker ps --format '{{.Names}}'); do
    [[ "$(docker inspect --format '{{range .HostConfig.DeviceRequests}}{{.Driver}}{{end}}' "$c" 2>/dev/null)" == *nvidia* ]] \
      || continue
    if says=$(docker exec "$c" nvidia-smi -L 2>&1); then
      ok "$c has the GPU"
    elif [[ "$says" != *"Failed to initialize NVML"* ]]; then
      continue
    elif docker restart "$c" >/dev/null && docker exec "$c" nvidia-smi -L >/dev/null 2>&1; then
      ok "$c had lost the GPU: restarted, it has the GPU again"
    else
      warn "$c has no GPU: try docker restart $c, then docker exec $c nvidia-smi"
    fi
  done
fi

# ---------------------------------------------------------------------------
step "Restarting services"
# The scheduler stops first: while the API restarts, its jobs couldn't save,
# and each would count against its clip as a failure.
systemctl is-enabled lumiverb-scheduler >/dev/null 2>&1 && systemctl stop lumiverb-scheduler
systemctl restart lumiverb-api
# uv's cache: an old Python's packages stay in it for good (prune keeps
# them), and the venv doesn't need it. Every update, so a rerun gets there.
as_svc "$UV_BIN" cache clean || warn "Couldn't clear uv's cache"

# A first start after new code compiles it (no .pyc yet): ~12 s on a 2-vCPU droplet (Oct 30), so up to 90 s,
# stopping at once if the unit fails.
ANSWERS=false
for i in {1..90}; do
  if curl -sf http://127.0.0.1:${API_PORT}/health >/dev/null 2>&1; then
    ANSWERS=true
    break
  fi
  systemctl is-failed --quiet lumiverb-api && break
  (( i % 10 == 0 )) && echo "  Waiting for the API to start (${i} s)..."
  sleep 1
done
[[ "$ANSWERS" == true ]] || fail "API server not responding after ${i} s — check: journalctl -u lumiverb-api -n 50"
# The plug-ins once the API answers (its migrations are done), the scheduler
# last: its jobs save through the API.
systemctl is-enabled lumiverb-plugins >/dev/null 2>&1 && systemctl restart lumiverb-plugins
systemctl is-enabled lumiverb-scheduler >/dev/null 2>&1 && systemctl start lumiverb-scheduler

systemctl status --no-pager lumiverb-api || true
systemctl is-enabled lumiverb-scheduler >/dev/null 2>&1 && systemctl status --no-pager lumiverb-scheduler || true

if curl -sf http://127.0.0.1:${API_PORT}/health >/dev/null 2>&1; then
  ok "API server healthy"
else
  fail "API server not responding — check: journalctl -u lumiverb-api -n 50"
fi

echo ""
echo -e "${GREEN}${BOLD}API update complete.${NC}"
