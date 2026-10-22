#!/usr/bin/env bash
# One-time setup script for new self-hosted Lumiverb installs. Idempotent: safe to run multiple times.
set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

API_URL="${API_URL:-http://localhost:8000}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-url)
      API_URL="${2:?Missing value for --api-url}"
      shift 2
      ;;
    *)
      echo -e "${RED}✗ Unknown option: $1${NC}" >&2
      echo "Usage: $0 [--api-url http://localhost:8000]" >&2
      exit 1
      ;;
  esac
done

echo "=== Checking dependencies ==="
missing=0
for cmd in docker uv lumiverb; do
  if command -v "$cmd" >/dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} $cmd"
  else
    echo -e "${RED}✗${NC} $cmd not found"
    missing=1
  fi
done
if [[ $missing -eq 1 ]]; then
  echo -e "${RED}Aborting: install missing tools and ensure 'lumiverb' (CLI) is on PATH.${NC}" >&2
  exit 1
fi

echo ""
echo "=== Checking Docker stack ==="
pg_status=$(docker compose ps --format json 2>/dev/null | python3 -c "
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        s = json.loads(line)
        if s.get('Name') == 'lumiverb-postgres':
            print(s.get('State', ''))
    except Exception:
        pass
")

if [[ "$pg_status" != "running" ]]; then
  echo -e "${RED}✗ lumiverb-postgres not running.${NC}" >&2
  echo "Run 'docker compose up -d postgres' first." >&2
  exit 1
fi
echo -e "${GREEN}✓${NC} lumiverb-postgres running"

echo "Waiting for Postgres to be ready (up to 30s)..."
for i in {1..30}; do
  if docker exec lumiverb-postgres pg_isready -U app -d control_plane 2>/dev/null; then
    echo -e "${GREEN}✓${NC} Postgres ready"
    break
  fi
  if [[ $i -eq 30 ]]; then
    echo -e "${RED}✗ Postgres did not become ready in time. Check 'docker compose logs postgres'.${NC}" >&2
    exit 1
  fi
  sleep 1
done

echo ""
echo "=== Ensuring ADMIN_KEY exists ==="
if [[ ! -f .env.local ]] || ! grep -q '^ADMIN_KEY=.\+' .env.local 2>/dev/null; then
  new_key=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  if [[ -f .env.local ]] && grep -q '^ADMIN_KEY=' .env.local 2>/dev/null; then
    # Written by Python, the key in its environment: never an argument (sed's), which every local user can see.
    NEW_KEY="$new_key" python3 -c '
import os, re
text = open(".env.local").read()
open(".env.local", "w").write(re.sub(r"(?m)^ADMIN_KEY=.*$", lambda _: "ADMIN_KEY=" + os.environ["NEW_KEY"], text))'
  else
    echo "ADMIN_KEY=$new_key" >> .env.local
  fi
  echo "Generated ADMIN_KEY and wrote to .env.local"
else
  echo -e "${GREEN}✓${NC} ADMIN_KEY already set in .env.local"
fi

echo ""
echo "=== Running control plane migrations ==="
if ! uv run alembic -c alembic-control.ini upgrade head; then
  echo -e "${RED}✗ Control plane migrations failed.${NC}" >&2
  exit 1
fi
echo "Control plane migrations up to date."

echo ""
echo "=== Checking if API server is running ==="
if ! curl -sf "$API_URL/health" >/dev/null; then
  echo -e "${YELLOW}⚠ The API server is not running. Start it with:${NC}"
  echo "  uv run fastapi dev src/api/main.py"
  echo "Then re-run this script."
  exit 0
fi
echo -e "${GREEN}✓${NC} API server reachable"

# Load ADMIN_KEY for admin API calls (strip optional quotes)
export ADMIN_KEY
ADMIN_KEY=$(grep '^ADMIN_KEY=' .env.local 2>/dev/null | sed 's/^ADMIN_KEY=//' | tr -d '"' | tr -d "'" | head -n1)
if [[ -z "${ADMIN_KEY:-}" ]]; then
  echo -e "${RED}✗ ADMIN_KEY not found in .env.local${NC}" >&2
  exit 1
fi

# The key reaches curl through a pipe (-H @file), never as an argument: every local user can see command lines.
admin_header() { printf 'Authorization: Bearer %s\n' "$ADMIN_KEY"; }

echo ""
echo "=== Checking if default tenant already exists ==="
tenants_json=$(curl -sf -H @<(admin_header) "$API_URL/v1/admin/tenants" || echo "[]")
has_default=$(TENANTS_JSON="$tenants_json" python3 -c "
import json, os
raw = os.environ.get('TENANTS_JSON', '[]')
try:
    data = json.loads(raw)
except Exception:
    data = []
for t in data:
    if t.get('name') == 'default':
        print('yes')
        break
else:
    print('no')
")

if [[ "$has_default" == "yes" ]]; then
  echo "Default tenant already exists."
  cli_config="${HOME}/.lumiverb/config.json"
  if [[ -f "$cli_config" ]]; then
    api_key_set=$(CLI_CONFIG="$cli_config" python3 -c "
import json, os
p = os.environ.get('CLI_CONFIG', '')
try:
    with open(p) as f:
        c = json.load(f)
    print('yes' if c.get('api_key') else 'no')
except Exception:
    print('no')
")
    if [[ "$api_key_set" == "yes" ]]; then
      echo -e "${GREEN}✓ CLI already configured.${NC}"
      echo ""
      echo -e "${GREEN}✓ Lumiverb is ready.${NC}"
      echo ""
      echo "Next steps:"
      echo "  lumiverb library create --name \"My Photos\" --path /path/to/your/photos"
      echo "  lumiverb scan --library \"My Photos\""
      echo "  (the scheduler, python -m src.server.scheduler, does the rest: docs/linux-server.md)"
      echo ""
      echo "API docs: $API_URL/docs"
      echo "Admin key: stored in .env.local"
      exit 0
    fi
  fi
  echo -e "${YELLOW}⚠ Warning: CLI not configured. Re-run after running:${NC}"
  echo "  lumiverb config set --api-key -   (it asks for the key, and doesn't show it)"
  echo "  (API key was shown when tenant was first created)"
  exit 0
fi

echo ""
echo "=== Creating default tenant ==="
create_resp=$(curl -s -w "\n%{http_code}" -X POST -H @<(admin_header) -H "Content-Type: application/json" \
  -d '{"name": "default", "plan": "free"}' "$API_URL/v1/admin/tenants")
http_code=$(echo "$create_resp" | tail -n1)
create_body=$(echo "$create_resp" | sed '$d')
if [[ "$http_code" -lt 200 ]] || [[ "$http_code" -ge 300 ]]; then
  echo -e "${RED}✗ Create tenant failed (HTTP $http_code):${NC}" >&2
  echo "$create_body" >&2
  exit 1
fi
api_key=$(python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
print(data.get('api_key', ''))
" <<< "$create_body")
if [[ -z "$api_key" ]]; then
  echo -e "${RED}✗ Create tenant response missing api_key: $create_body${NC}" >&2
  exit 1
fi

echo ""
echo "=== Configuring CLI ==="
printf '%s\n' "$api_key" | lumiverb config set --api-url "$API_URL" --api-key -  # the key on its stdin
echo "CLI configured."

echo ""
echo -e "${GREEN}✓ Lumiverb is ready.${NC}"
echo ""
echo "Next steps:"
echo "  lumiverb library create --name \"My Photos\" --path /path/to/your/photos"
echo "  lumiverb scan --library \"My Photos\""
echo "  (the scheduler, python -m src.server.scheduler, does the rest: docs/linux-server.md)"
echo ""
echo "API docs: $API_URL/docs"
echo "Admin key: stored in .env.local"
