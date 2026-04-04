#!/usr/bin/env bash
# Bootstrap Lumiverb on a fresh Ubuntu 22.04+ VPS (single-machine deploy).
#
# This is a convenience wrapper that runs deploy-api.sh then deploy-web.sh
# on the same machine. For split deployments (API and web on separate machines),
# run those scripts individually.
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
#   sudo -u lumiverb /opt/lumiverb/.venv/bin/lumiverb user create --email you@example.com --role admin
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
GREEN='\033[0;32m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

step()  { echo -e "\n${BOLD}=== $1 ===${NC}"; }
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

# Collect all args to forward to deploy-api.sh
ALL_ARGS=("$@")

# Extract --domain and web-specific args for deploy-web.sh
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain)         DOMAIN="${2:-}"; shift 2 ;;
    --email)          CERTBOT_EMAIL="${2:-}"; shift 2 ;;
    --certificate)    CERTIFICATE_ARCHIVE="${2:-}"; shift 2 ;;
    --skip-certbot)   SKIP_CERTBOT=true; shift ;;
    *)                shift ;;  # skip other args (forwarded to deploy-api.sh)
  esac
done

[[ -n "$DOMAIN" ]] || fail "Required: --domain <FQDN>  (e.g. --domain app.example.com)"
[[ "$(id -u)" -eq 0 ]] || fail "This script must be run as root (try: sudo bash ...)"

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

  step "Bootstrapping repo for script access"
  if [[ -d "${APP_DIR}/.git" ]]; then
    cd "$APP_DIR"
    git fetch --all --prune
    git checkout "$BRANCH"
    git pull origin "$BRANCH"
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

bash "${SCRIPT_DIR}/deploy-api.sh" "${API_ARGS[@]}"

# ---------------------------------------------------------------------------
# Phase 2: Deploy Web
# ---------------------------------------------------------------------------
step "Deploying Web UI (Phase 2 of 2)"

WEB_ARGS=(--domain "$DOMAIN" --api-upstream "http://127.0.0.1:8000")
[[ -n "$CERTBOT_EMAIL" ]]     && WEB_ARGS+=(--email "$CERTBOT_EMAIL")
[[ -n "$CERTIFICATE_ARCHIVE" ]] && WEB_ARGS+=(--certificate "$CERTIFICATE_ARCHIVE")
[[ "$SKIP_CERTBOT" == "true" ]] && WEB_ARGS+=(--skip-certbot)

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

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}Lumiverb fully deployed (API + Web on single machine).${NC}"
echo ""
echo "To update:  bash /opt/lumiverb/scripts/update-vps.sh"
echo "API only:   bash /opt/lumiverb/scripts/update-api.sh"
echo "Web only:   bash /opt/lumiverb/scripts/update-web.sh"
echo ""
