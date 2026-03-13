#!/usr/bin/env bash
# Run Alembic migrations for control plane + all tenant databases.
# Idempotent: safe to run multiple times.
set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# --- Load CONTROL_PLANE_DATABASE_URL from .env.local or environment ---
if [[ -z "${CONTROL_PLANE_DATABASE_URL:-}" ]]; then
  if [[ -f .env.local ]]; then
    val=$(grep '^CONTROL_PLANE_DATABASE_URL=' .env.local 2>/dev/null \
      | sed 's/^CONTROL_PLANE_DATABASE_URL=//' | tr -d '"' | tr -d "'" | head -n1)
    if [[ -n "$val" ]]; then
      export CONTROL_PLANE_DATABASE_URL="$val"
    fi
  fi
fi
if [[ -z "${CONTROL_PLANE_DATABASE_URL:-}" ]]; then
  echo -e "${RED}✗ CONTROL_PLANE_DATABASE_URL not set and not found in .env.local${NC}" >&2
  exit 1
fi

# Use same URL for Alembic (control plane env.py reads ALEMBIC_CONTROL_URL)
export ALEMBIC_CONTROL_URL="${CONTROL_PLANE_DATABASE_URL}"

echo "=== Step 1: Control plane migrations ==="
if ! uv run alembic -c alembic-control.ini upgrade head; then
  echo -e "${RED}✗ Control plane migration failed.${NC}" >&2
  exit 1
fi
echo -e "${GREEN}✓ Control plane up to date${NC}"

echo ""
echo "=== Step 2: Enumerating tenants ==="
tenant_urls=$(uv run python - <<'PYEOF'
import os, sys
url = os.environ.get("CONTROL_PLANE_DATABASE_URL", "")
if not url:
    print("", end="")
    sys.exit(0)
# psycopg2 expects postgresql://, not postgresql+psycopg2://
if url.startswith("postgresql+psycopg2://"):
    url = "postgresql://" + url[len("postgresql+psycopg2://"):]
try:
    import psycopg2
    conn = psycopg2.connect(url)
    cur = conn.cursor()
    cur.execute("SELECT tenant_id, connection_string FROM tenant_db_routing ORDER BY tenant_id")
    rows = cur.fetchall()
    for tenant_id, conn_str in rows:
        print(f"{tenant_id}\t{conn_str}")
    conn.close()
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
)

if [[ -z "$tenant_urls" ]]; then
  echo -e "${YELLOW}⚠ No tenants found — skipping tenant migrations.${NC}"
  exit 0
fi

tenant_count=$(echo "$tenant_urls" | wc -l | tr -d ' ')
echo "Found $tenant_count tenant(s)"

echo ""
echo "=== Step 3: Tenant migrations ==="
failed=0
current=0
while IFS=$'\t' read -r tenant_id conn_str; do
  current=$((current + 1))
  echo -n "  [$current/$tenant_count] $tenant_id ... "
  if ALEMBIC_TENANT_URL="$conn_str" uv run alembic -c alembic-tenant.ini upgrade head \
      > /tmp/lumiverb_alembic_tenant.log 2>&1; then
    echo -e "${GREEN}✓${NC}"
  else
    echo -e "${RED}✗ FAILED${NC}"
    cat /tmp/lumiverb_alembic_tenant.log >&2
    failed=$((failed + 1))
  fi
done <<< "$tenant_urls"

echo ""
if [[ $failed -gt 0 ]]; then
  echo -e "${RED}✗ $failed tenant migration(s) failed.${NC}" >&2
  exit 1
fi
echo -e "${GREEN}✓ All tenant migrations complete.${NC}"
