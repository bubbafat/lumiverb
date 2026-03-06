# Lumiverb API — Cursor Context
*Feed this to Cursor when working on the API server. These decisions are final — do not relitigate them.*

---

## Stack

- Python 3.12
- FastAPI + SQLModel + Alembic
- PostgreSQL 16 (per-tenant database model)
- Quickwit (BM25 search)
- Object storage via abstraction layer (GCS / S3 / B2 / MinIO)

## Non-Negotiables

- Every route lives under `/v1/`
- All routes require `Authorization: Bearer {api_key}` header
- Tenant context is always derived from the API key — never passed as a URL param or body field
- Pagination is cursor-based, never offset-based
- File uploads are multipart form data
- File serving uses signed URLs (cloud) or direct proxy (self-hosted) — never expose raw object storage URLs
- Error responses always use this envelope: `{"error": {"code": "...", "message": "...", "details": {...}}}`
- OpenAPI spec is generated automatically from route definitions — keep docstrings accurate

## Database Model

Two-layer Postgres architecture:

**Control plane DB** (shared, tiny):
- `tenants` — tenant_id, name, plan, status, created_at
- `api_keys` — key_hash, tenant_id, name, scopes, created_at  
- `tenant_db_routing` — tenant_id, connection_string, region

**Tenant DB** (one per tenant, same Postgres instance):
- `libraries` — library_id, name, root_path, scan_status, created_at
- `assets` — asset_id, library_id, sha256, file_path, file_size, media_type, width, height, duration_ms, captured_at, proxy_key, thumbnail_key, availability, created_at
- `video_scenes` — scene_id, asset_id, start_ms, end_ms, rep_frame_ms, proxy_key, thumbnail_key
- `asset_metadata` — asset_id, exif_json, sharpness_score, face_count, ai_description, ai_description_at, embedding_vector vector(512) [nullable]
- `search_sync_queue` — asset_id, scene_id, operation, status, created_at
- `worker_jobs` — job_id, job_type, asset_id, status, worker_id, claimed_at, completed_at, error
- `system_metadata` — key, value
- `faces` — face_id, asset_id, bounding_box_json, embedding_vector vector(512), detection_confidence, created_at [phase 2, empty until then]
- `people` — person_id, display_name, created_by_user, created_at [phase 2]
- `face_person_matches` — face_id, person_id, confidence, confirmed bool, confirmed_at [phase 2]

pgvector extension is enabled at provisioning time. The `vector(512)` columns exist from day one but are nullable and unpopulated until phase 2 workers run. Do not query these columns in phase 1 code.

When writing queries, always use the tenant DB session, not the control plane session. The middleware resolves this from the API key before the route handler runs.

## Admin API

Admin routes live under `/v1/admin` and require `Authorization: Bearer {ADMIN_KEY}` (not tenant API keys). If `ADMIN_KEY` is not set, admin routes return 500.

- **POST /v1/admin/tenants** — Body: `{ "name", "plan": "free|pro|enterprise", "email" }`. Creates tenant, provisions tenant DB (pgvector + Alembic), creates routing row, creates default API key. Returns `{ "tenant_id", "api_key", "database": "provisioned" }`. On failure, cleans up and returns 500.
- **GET /v1/admin/tenants** — Returns list of tenants with `tenant_id`, `name`, `plan`, `status` (no API keys).
- **DELETE /v1/admin/tenants/{tenant_id}** — Soft delete: sets tenant status to `deleted`, revokes all API keys. Returns 204.

## Worker Job Pattern

Workers use lease-based claiming to prevent duplicate processing:

```python
# Claim a job — sets status='claimed', claimed_at=now(), worker_id=worker_id
# Job must be completed or released within lease_duration (default 5 min)
# Expired leases are automatically reclaimed by the next worker poll
GET /v1/jobs/claim?type={job_type}

# Complete a job
POST /v1/jobs/{job_id}/complete

# Release a job back to the queue (on worker error)
POST /v1/jobs/{job_id}/release
```

Worker types in v1: `vision`, `video`, `metadata`. Type `face` is reserved for phase 2 — do not implement.

## SHA256 Deduplication

When an asset is submitted via POST /v1/assets:
1. Check if sha256 already exists in tenant DB
2. If yes — return existing asset_id with 200 (not 201), do not re-store
3. If no — create new asset record, store proxy/thumbnail, enqueue jobs

## Search

Search is BM25 via Quickwit. The API queries Quickwit then enriches results with Postgres metadata. Never query Postgres for full-text search.

Similarity search uses the asset's AI description as a BM25 query — not embeddings (v1). Exclude the source asset from results. Apply adaptive threshold based on corpus size.

## File Serving

- Thumbnails: fast, served from signed URLs, used in grid views
- Proxies: larger, used for AI inference and detail views, signed URLs
- Source files: NEVER served via the API under any circumstances

## Migrations

Alembic manages schema. Two migration contexts:
- `control_plane` — migrations for the shared control plane DB
- `tenant` — migrations applied to each tenant DB on provisioning and upgrade

When adding a column or table to the tenant schema, always add a corresponding Alembic migration in the tenant context.

## Environment Variables

```
DATABASE_CONTROL_URL=postgresql://...
OBJECT_STORAGE_BACKEND=gcs|s3|b2|minio
OBJECT_STORAGE_BUCKET=...
OBJECT_STORAGE_CREDENTIALS=...
QUICKWIT_URL=http://quickwit:7280
API_KEY_SALT=...
ENVIRONMENT=development|production
```

## What Not to Build

- Do not add user authentication (login/password/sessions) — that is Phase 5
- Do not add webhooks — that is future work
- Do not add multi-library search in v1 — search is per-library
- Do not store or serve source files
- Do not add rate limiting in v1 — that is future work
- Do not populate or query `embedding_vector`, `faces`, `people`, or `face_person_matches` — phase 2 only
- Do not implement face clustering or identity assignment — phase 2 only
