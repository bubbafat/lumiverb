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
#   5. Fix Quickwit sandbox
#   6. Install upkeep timers
#   7. Restart API + Quickwit + scheduler
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
ENV_FILE="/etc/lumiverb/env"
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

[[ "$(id -u)" -eq 0 ]] || fail "Run as root"
[[ -d "${APP_DIR}/.git" ]] || fail "${APP_DIR} is not a git repo — run deploy-api.sh first"

# Ensure the service user's HOME exists.
SVC_HOME="$(getent passwd "$SVC_USER" | cut -d: -f6)"
if [[ -n "$SVC_HOME" ]] && [[ ! -d "$SVC_HOME" ]]; then
  mkdir -p "$SVC_HOME"
  chown "$SVC_USER":"$SVC_USER" "$SVC_HOME"
fi

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
  exec bash "$APP_DIR/scripts/update-api.sh" "$@"
fi

# ---------------------------------------------------------------------------
step "Updating Python dependencies"
EXTRAS=(--extra cli --extra embeddings --extra face_recognition)
# uv sync removes what the extras don't list, so keep processing's (the
# scheduler's, or the worker's it replaces).
PROCESSING=false
if systemctl is-enabled lumiverb-scheduler >/dev/null 2>&1 || systemctl is-enabled lumiverb-worker >/dev/null 2>&1; then
  PROCESSING=true
  EXTRAS+=(--extra workers)
  # Before anything changes: the scheduler's caches need the data disk.
  grep -q '^DATA_DIR=.' "$ENV_FILE" || fail "No DATA_DIR in ${ENV_FILE}: the scheduler's caches need the data disk"
fi
# A new Python makes uv sync delete the venv and download it all again. So
# download first, into a side venv, while the API and scheduler run; then stop
# them only for the swap, which the warm cache makes quick.
HAVE_PY="$(sed -n 's/^version_info *= *\([0-9]*\.[0-9]*\).*/\1/p' "$APP_DIR/.venv/pyvenv.cfg" 2>/dev/null || true)"
WANT_PY="$(grep -oE '[0-9]+\.[0-9]+' "$APP_DIR/.python-version" 2>/dev/null | head -1 || true)"
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
  sudo -u "$SVC_USER" "$UV_BIN" python install "$WANT_PY"
  rm -rf "$APP_DIR/.venv-next"  # uv won't build into what an interrupted run left
  sudo -u "$SVC_USER" env UV_PROJECT_ENVIRONMENT="$APP_DIR/.venv-next" "$UV_BIN" sync "${EXTRAS[@]}"
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
sudo -u "$SVC_USER" "$UV_BIN" sync "${EXTRAS[@]}"
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

DB_URL="$(grep '^CONTROL_PLANE_DATABASE_URL=' "$ENV_FILE" | cut -d= -f2-)"
[[ -n "$DB_URL" ]] || fail "CONTROL_PLANE_DATABASE_URL not found in ${ENV_FILE}"

export ALEMBIC_CONTROL_URL="$DB_URL"
sudo -u "$SVC_USER" --preserve-env=ALEMBIC_CONTROL_URL \
  "$APP_DIR/.venv/bin/python" -m alembic -c alembic-control.ini upgrade head
ok "Control plane migrations applied"

export CONTROL_PLANE_DATABASE_URL="$DB_URL"
export UV_BIN
sudo -u "$SVC_USER" --preserve-env=CONTROL_PLANE_DATABASE_URL,ALEMBIC_CONTROL_URL,UV_BIN \
  bash "$APP_DIR/scripts/migrate.sh"
ok "Tenant migrations applied"

# ---------------------------------------------------------------------------
step "Scheduler settings"
# The scheduler reads its own settings (LUMIVERB_* in this env file), never
# the CLI's config. What it read from the service user's CLI config before
# (library roots, how video is rendered) is carried over once; a setting
# already here stays. Before anything saves that config (config set, below),
# which keeps only the CLI's own keys.
if [[ "$PROCESSING" == "true" ]]; then
  python3 "$APP_DIR/scripts/scheduler-env.py" "$ENV_FILE" --from-cli-config "${SVC_HOME}/.lumiverb/config.json"
  grep -q '^LUMIVERB_ROOT_MAP=' "$ENV_FILE" \
    || warn "No LUMIVERB_ROOT_MAP in ${ENV_FILE}: libraries are read at the paths the server stores"
  ok "Scheduler settings in ${ENV_FILE}"
else
  ok "No processing on this machine"
fi

# ---------------------------------------------------------------------------
step "Ensuring data directory"
DATA_DIR="$(grep '^DATA_DIR=' "$ENV_FILE" | cut -d= -f2- || true)"
if [[ -n "$DATA_DIR" ]]; then
  mkdir -p "$DATA_DIR"/quickwit "$DATA_DIR"/tmp "$DATA_DIR"/worker-tmp
  chown -R "$SVC_USER":"$SVC_USER" "$DATA_DIR"
  # Caches, and the scheduler's lock and state, on the data disk for manual
  # runs as the service user too, so one never runs beside the service.
  grep -q '^XDG_CACHE_HOME=' "$ENV_FILE" || env_append "XDG_CACHE_HOME=${DATA_DIR}/cache"
  sudo -u "$SVC_USER" -H "$APP_DIR/.venv/bin/lumiverb" config set --cache-home "${DATA_DIR}/cache" >/dev/null
  ok "Data dir: $DATA_DIR"
fi

# ---------------------------------------------------------------------------
step "Fixing Quickwit sandbox (namespace-dependent directives)"
QW_UNIT="/etc/systemd/system/lumiverb-quickwit.service"
if [[ -f "$QW_UNIT" ]] && grep -qE '^(PrivateTmp|ProtectSystem|ReadWritePaths|ReadOnlyPaths)=' "$QW_UNIT"; then
  sed -i '/^PrivateTmp=/d; /^ProtectSystem=/d; /^ReadWritePaths=/d; /^ReadOnlyPaths=/d' "$QW_UNIT"
  systemctl daemon-reload
  ok "Removed namespace-dependent directives from Quickwit unit"
fi

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
fi

# ---------------------------------------------------------------------------
step "Installing the scheduler (all processing; it replaces the worker)"
if [[ "$PROCESSING" == "true" ]]; then
  DATA_DIR="$(grep '^DATA_DIR=' "$ENV_FILE" | cut -d= -f2- || true)"
  [[ -n "$DATA_DIR" ]] || fail "No DATA_DIR in ${ENV_FILE}: the scheduler's caches need the data disk"
  mkdir -p "$DATA_DIR"/worker-tmp "$DATA_DIR"/cache
  chown "$SVC_USER":"$SVC_USER" "$DATA_DIR"/worker-tmp "$DATA_DIR"/cache
  # Rewritten each update: everything it needs is known here.
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
ReadWritePaths=${DATA_DIR} ${SVC_HOME}

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
step "Installing upkeep timers"
cat > /etc/systemd/system/lumiverb-upkeep.service <<UPKEEP_SVC
[Unit]
Description=Lumiverb periodic upkeep (search sync, cleanup)

[Service]
Type=oneshot
User=${SVC_USER}
Group=${SVC_USER}
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/curl -sf -X POST "http://127.0.0.1:\${API_PORT}/v1/upkeep?tenants=all" -H "Authorization: Bearer \${ADMIN_KEY}" -H "Content-Type: application/json"
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
ExecStart=/usr/bin/curl -sf -X POST "http://127.0.0.1:\${API_PORT}/v1/upkeep/cleanup?tenants=all&dry_run=false" -H "Authorization: Bearer \${ADMIN_KEY}" -H "Content-Type: application/json"
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
# cgroups): the brain's Ollama then runs its model on the CPU, with no error,
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
systemctl is-enabled lumiverb-quickwit >/dev/null 2>&1 && systemctl restart lumiverb-quickwit
systemctl restart lumiverb-api
# uv's cache: an old Python's packages stay in it for good (prune keeps
# them), and the venv doesn't need it. Every update, so a rerun gets there.
sudo -u "$SVC_USER" "$UV_BIN" cache clean || warn "Couldn't clear uv's cache"

ANSWERS=false
for i in {1..10}; do
  if curl -sf http://127.0.0.1:${API_PORT}/health >/dev/null 2>&1; then
    ANSWERS=true
    break
  fi
  sleep 1
done
[[ "$ANSWERS" == true ]] || fail "API server not responding after 10 s — check: journalctl -u lumiverb-api -n 50"
# The scheduler last, once the API answers: its jobs save through the API.
systemctl is-enabled lumiverb-scheduler >/dev/null 2>&1 && systemctl start lumiverb-scheduler

systemctl status --no-pager lumiverb-api || true
systemctl is-enabled lumiverb-quickwit >/dev/null 2>&1 && systemctl status --no-pager lumiverb-quickwit || true
systemctl is-enabled lumiverb-scheduler >/dev/null 2>&1 && systemctl status --no-pager lumiverb-scheduler || true

if curl -sf http://127.0.0.1:${API_PORT}/health >/dev/null 2>&1; then
  ok "API server healthy"
else
  fail "API server not responding — check: journalctl -u lumiverb-api -n 50"
fi

echo ""
echo -e "${GREEN}${BOLD}API update complete.${NC}"
