#!/usr/bin/env bash
# Update an existing Lumiverb VPS deployment (single-machine).
#
# This is a convenience wrapper that runs update-api.sh then update-web.sh
# on the same machine. For split deployments, run those scripts individually
# on their respective machines.
#
# Usage (from VPS):
#   bash /opt/lumiverb/scripts/update-vps.sh
#
# Or remote:
#   ssh root@your-vps 'bash /opt/lumiverb/scripts/update-vps.sh'
#
set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

step()  { echo -e "\n${BOLD}=== $1 ===${NC}"; }
fail()  { echo -e "${RED}  ✗ $1${NC}" >&2; exit 1; }

APP_DIR="/opt/lumiverb"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

[[ "$(id -u)" -eq 0 ]] || fail "Run as root"
[[ -d "${APP_DIR}/.git" ]] || fail "${APP_DIR} is not a git repo — run deploy-vps.sh first"

# ---------------------------------------------------------------------------
step "Updating API"
bash "${SCRIPT_DIR}/update-api.sh"

# ---------------------------------------------------------------------------
# Only run web update if nginx is installed (supports API-only machines
# that were originally deployed with deploy-vps.sh but later split)
if command -v nginx >/dev/null 2>&1 && [[ -d "${APP_DIR}/src/ui/web" ]]; then
  step "Updating Web UI"
  bash "${SCRIPT_DIR}/update-web.sh"
else
  echo ""
  echo "  (nginx not found — skipping web update)"
fi

# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}Update complete.${NC}"
