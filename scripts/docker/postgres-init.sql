-- Runs once on first container start (docker-entrypoint-initdb.d).
-- Enables pgvector extension on the control plane DB.
-- Tenant databases get their own pgvector enable via the provisioning API.

CREATE EXTENSION IF NOT EXISTS vector;

-- Confirm pgvector is available (visible in docker logs on startup)
DO $$
BEGIN
  RAISE NOTICE 'pgvector extension enabled: %', (SELECT extversion FROM pg_extension WHERE extname = 'vector');
END $$;
