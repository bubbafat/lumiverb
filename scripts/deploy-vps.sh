#!/usr/bin/env bash
# Bootstrap Lumiverb on a fresh Ubuntu 22.04+ VPS (single-machine deploy).
#
# This is a convenience wrapper that runs deploy-api.sh then deploy-web.sh
# on the same machine. For split deployments (API and web on separate machines),
# run those scripts individually.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/bubbafat/lumiverb/main/scripts/deploy-vps.sh \
#     | sudo bash -s -- --domain app.example.com --email you@example.com --worker
#
# Or from a local checkout:
#   sudo bash scripts/deploy-vps.sh --domain app.example.com
#
# Idempotent: safe to run again to update an existing install. --dry-run
# prints the settings both halves would use and changes nothing.
#
# After completion, create the first admin (while nobody can log in):
#   sudo /opt/lumiverb/.venv/bin/lumiverb-server setup --env /etc/lumiverb/env
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
GREEN='\033[0;32m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

# A dry run's stdout is the settings alone (KEY=value); its headings go to stderr.
step()  { if [[ "$DRY_RUN" == "true" ]]; then echo -e "\n${BOLD}=== $1 ===${NC}" >&2; else echo -e "\n${BOLD}=== $1 ===${NC}"; fi; }
fail()  { echo -e "${RED}  ✗ $1${NC}" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Resolve script directory (handles curl-pipe and local invocations)
# ---------------------------------------------------------------------------
# When run from a local checkout, use sibling scripts directly.
# When run via curl-pipe, clone the repo first then call the scripts.
SCRIPT_DIR=""
if [[ -f "$(dirname "${BASH_SOURCE[0]:-$0}")/deploy-api.sh" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
fi

# ---------------------------------------------------------------------------
# Separate args for API and web scripts
# ---------------------------------------------------------------------------
DOMAIN=""
CERTBOT_EMAIL=""
SKIP_CERTBOT=false
CERTIFICATE_ARCHIVE=""
FIREWALL_FLAG=""
DRY_RUN=false

# Collect all args to forward to deploy-api.sh
ALL_ARGS=("$@")

# Extract --domain and web-specific args for deploy-web.sh
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain)         DOMAIN="${2:-}"; shift 2 ;;
    --email)          CERTBOT_EMAIL="${2:-}"; shift 2 ;;
    --certificate)    CERTIFICATE_ARCHIVE="${2:-}"; shift 2 ;;
    --skip-certbot)   SKIP_CERTBOT=true; shift ;;
    --no-firewall|--firewall) FIREWALL_FLAG="$1"; shift ;;  # both halves
    --dry-run)        DRY_RUN=true; shift ;;                 # both halves
    *)                shift ;;  # skip other args (forwarded to deploy-api.sh)
  esac
done

[[ -n "$DOMAIN" ]] || fail "Required: --domain <FQDN>  (e.g. --domain app.example.com)"
# A dry run reads the remembered settings, so it too needs sudo where there
# are some: each script says so.
[[ "$DRY_RUN" == "true" || "$(id -u)" -eq 0 ]] || fail "This script must be run as root (try: sudo bash ...)"

# ---------------------------------------------------------------------------
# If scripts not found locally, we need to bootstrap
# ---------------------------------------------------------------------------
APP_DIR="/opt/lumiverb"
if [[ -z "$SCRIPT_DIR" ]]; then
  # Running via curl-pipe — extract --repo and --branch from args if present
  REPO_URL="https://github.com/bubbafat/lumiverb.git"
  BRANCH="main"
  set -- "${ALL_ARGS[@]}"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --repo)   REPO_URL="${2:-}"; shift 2 ;;
      --branch) BRANCH="${2:-}"; shift 2 ;;
      *)        shift ;;
    esac
  done

  if [[ "$DRY_RUN" == "true" ]]; then
    # Changes nothing: the scripts come from a throwaway clone, not /opt/lumiverb.
    command -v git >/dev/null 2>&1 || fail "A dry run needs git (apt-get install git)"
    CLONE="$(mktemp -d)"
    trap 'rm -rf "$CLONE"' EXIT
    git clone -q --depth 1 --branch "$BRANCH" "$REPO_URL" "$CLONE" >&2
    SCRIPT_DIR="${CLONE}/scripts"
  fi
fi
if [[ -z "$SCRIPT_DIR" ]]; then
  step "Bootstrapping repo for script access"
  if [[ -d "${APP_DIR}/.git" ]]; then
    # git runs as whoever owns the checkout (the service user, once deploy-api.sh has run), without that user's own
    # git settings: never as root in a checkout another user can change.
    OWNER="$(stat -c %U "$APP_DIR")"
    cd "$APP_DIR"
    sudo -u "$OWNER" env GIT_CONFIG_GLOBAL=/dev/null git fetch --all --prune
    sudo -u "$OWNER" env GIT_CONFIG_GLOBAL=/dev/null git checkout "$BRANCH"
    sudo -u "$OWNER" env GIT_CONFIG_GLOBAL=/dev/null git pull origin "$BRANCH"
  else
    apt-get update -qq && apt-get install -y -qq git
    git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  fi
  SCRIPT_DIR="${APP_DIR}/scripts"
fi

# ---------------------------------------------------------------------------
# Phase 1: Deploy API
# ---------------------------------------------------------------------------
step "Deploying API (Phase 1 of 2)"

# Build API args: forward everything except web-specific flags
API_ARGS=()
set -- "${ALL_ARGS[@]}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --certificate)  shift 2 ;;  # web-only
    --skip-certbot) shift ;;    # web-only
    *)              API_ARGS+=("$1"); shift ;;
  esac
done

# The web half proxies to where the API half settled: --api-port and
# --api-listen-host, else what the env file remembers, else the defaults.
CONF_DIR="${LUMIVERB_CONF_DIR:-/etc/lumiverb}"
if [[ "$DRY_RUN" == "true" ]]; then
  API_SETTINGS="$(bash "${SCRIPT_DIR}/deploy-api.sh" "${API_ARGS[@]}")"
  echo "$API_SETTINGS"
else
  bash "${SCRIPT_DIR}/deploy-api.sh" "${API_ARGS[@]}"
  API_SETTINGS="$(grep -E '^API_(PORT|LISTEN_HOST)=' "${CONF_DIR}/env" || true)"
fi
API_PORT="$(sed -n 's/^API_PORT=//p' <<<"$API_SETTINGS" | head -1)"
API_HOST="$(sed -n 's/^API_LISTEN_HOST=//p' <<<"$API_SETTINGS" | head -1)"
[[ -n "$API_PORT" ]] || fail "No API_PORT from deploy-api.sh (${CONF_DIR}/env)"
case "$API_HOST" in
  ""|0.0.0.0|::|"[::]") API_HOST="127.0.0.1" ;;  # every address: this machine's own will do
esac
[[ "$API_HOST" != *:* || "$API_HOST" == \[* ]] || API_HOST="[${API_HOST}]"  # an IPv6 address

# ---------------------------------------------------------------------------
# Phase 2: Deploy Web
# ---------------------------------------------------------------------------
step "Deploying Web UI (Phase 2 of 2)"

WEB_ARGS=(--domain "$DOMAIN" --api-upstream "http://${API_HOST}:${API_PORT}")
[[ -n "$CERTBOT_EMAIL" ]]     && WEB_ARGS+=(--email "$CERTBOT_EMAIL")
[[ -n "$CERTIFICATE_ARCHIVE" ]] && WEB_ARGS+=(--certificate "$CERTIFICATE_ARCHIVE")
[[ "$SKIP_CERTBOT" == "true" ]] && WEB_ARGS+=(--skip-certbot)
[[ -n "$FIREWALL_FLAG" ]]     && WEB_ARGS+=("$FIREWALL_FLAG")
[[ "$DRY_RUN" == "true" ]]    && WEB_ARGS+=(--dry-run)

# Forward --repo and --branch if present
set -- "${ALL_ARGS[@]}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)   WEB_ARGS+=(--repo "${2:-}"); shift 2 ;;
    --branch) WEB_ARGS+=(--branch "${2:-}"); shift 2 ;;
    *)        shift ;;
  esac
done

bash "${SCRIPT_DIR}/deploy-web.sh" "${WEB_ARGS[@]}"
[[ "$DRY_RUN" != "true" ]] || exit 0

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}Lumiverb fully deployed (API + Web on single machine).${NC}"
echo ""
echo "Create your account (while nobody can sign in):"
echo "  sudo ${APP_DIR}/.venv/bin/lumiverb-server setup --env ${CONF_DIR}/env"
echo ""
echo "Update:"
echo "  sudo bash ${APP_DIR}/scripts/update.sh"
echo ""
