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

Tenant resolution runs for every request except `/health` and `/v1/admin/*`: reads `Authorization: Bearer <token>`, validates via control plane `ApiKeyRepository.get_by_plaintext`, touches `last_used_at`, looks up `TenantDbRouting` for the connection string, and stores `tenant_id` and `connection_string` in `request.state`. Use the `get_tenant_session` dependency in route handlers to obtain a tenant DB session.

## Tenant Context

- **GET /v1/tenant/context** — Tenant auth required. Returns `{ "tenant_id" }` only. Used by CLI/worker for storage path computation. Workers must not have direct DB access; they use the jobs API only.

## Jobs API

All under `/v1/jobs`; require tenant auth.

- **POST /v1/jobs/enqueue** — Body: `{ "library_id", "job_type" }`. Only `job_type: "proxy"` is supported for now. Creates worker_jobs for all pending assets in the library that don’t already have a pending/claimed proxy job. Returns `{ "enqueued" }` (count of jobs created).
- **GET /v1/jobs/next** — Query: `job_type` (required), `library_id` (optional). Claims next pending job; returns 204 if none. On success returns `{ "job_id", "job_type", "asset_id", "rel_path", "media_type", "library_id", "root_path" }`. 404 if asset or library not found (job is failed server-side).
- **POST /v1/jobs/{job_id}/complete** — Body for proxy: `{ "proxy_key", "thumbnail_key", "width", "height" }`. Marks job completed; for proxy jobs updates asset. Returns `{ "job_id", "status": "completed" }`. 404 if job not found, 409 if job not claimed.
- **POST /v1/jobs/{job_id}/fail** — Body: `{ "error_message" }`. Marks job failed. Returns `{ "job_id", "status": "failed" }`.
- **GET /v1/jobs** — Query: `library_id` (optional). List jobs; filter by library when provided. Returns list of `{ "job_id", "job_type", "asset_id", "status" }`.
- **GET /v1/jobs/{job_id}/status** — Returns `{ "job_id", "status", "error_message" }`. 404 if not found.

## Libraries API

All under `/v1/libraries`; require tenant auth (middleware).

- **POST /v1/libraries** — Body: `{ "name", "root_path" }`. Name must be unique per tenant (409 if duplicate). Returns `{ "library_id", "name", "root_path", "scan_status" }` (scan_status initially `"idle"`).
- **GET /v1/libraries** — Query: `include_trashed` (optional, default false). Returns list of libraries with `library_id`, `name`, `root_path`, `scan_status`, `last_scan_at`, `status` (`"active"` or `"trashed"`). Trashed libraries excluded unless `include_trashed=true`.
- **DELETE /v1/libraries/{library_id}** — Soft delete: set library `status` to `"trashed"`, cancel pending/claimed worker jobs for its assets. Returns 204 on success, 404 if not found, 409 if already trashed.
- **POST /v1/libraries/empty-trash** — Hard delete all trashed libraries for this tenant (cascade: worker_jobs, search_sync_queue, asset_metadata, video_scenes, assets, scans, libraries). Returns `{ "deleted": N }`.

## Scans API

All under `/v1/scans`; require tenant auth.

- **POST /v1/scans** — Body: `{ "library_id", "status": "running|aborted|error", "root_path_override": null, "worker_id": null, "error_message": null }`. Creates scan record; if status is `running` sets library `scan_status` to `"scanning"`; if `aborted` or `error` updates library `scan_status` and `last_scan_error`. Returns `{ "scan_id" }`.
- **GET /v1/scans/running?library_id=** — Returns list of running scans: `{ "scan_id", "library_id", "started_at", "worker_id" }`.
- **POST /v1/scans/{scan_id}/complete** — Body: `{ "files_discovered", "files_added", "files_updated", "files_skipped" }`. Marks assets not seen in this scan as missing, completes scan, updates library `scan_status` and `last_scan_at`. Returns `{ "scan_id", "files_missing" }`.
- **POST /v1/scans/{scan_id}/abort** — Body: `{ "error_message": null }`. Aborts scan, updates library `scan_status` to `"error"` or `"aborted"`. Returns `{ "scan_id", "status" }`.

## Assets API

All under `/v1/assets`; require tenant auth.

- **GET /v1/assets** — Query: `library_id` (optional). List assets; filter by library when provided. Returns list of `{ "asset_id", "library_id", "rel_path", "media_type", "status", "proxy_key", "thumbnail_key", "width", "height" }`.
- **GET /v1/assets/{asset_id}** — Return single asset. 404 if not found.
- **POST /v1/assets/upsert** — Body: `{ "library_id", "rel_path", "file_size", "file_mtime" (ISO8601), "media_type", "scan_id", "force": false }`. Upserts by `(library_id, rel_path)`: create if not found (`action: "added"`); if found and `force` or size/mtime/sha256 changed then update (`action: "updated"`); if found and unchanged (sha256 set, size/mtime same) then touch only (`action: "skipped"`). Returns `{ "action": "added|updated|skipped" }`.

## Admin API

Admin routes live under `/v1/admin` and require `Authorization: Bearer {ADMIN_KEY}` (not tenant API keys). If `ADMIN_KEY` is not set, admin routes return 500.

- **POST /v1/admin/tenants** — Body: `{ "name", "plan": "free|pro|enterprise", "email" }`. Creates tenant, provisions tenant DB (pgvector + Alembic), creates routing row, creates default API key. Returns `{ "tenant_id", "api_key", "database": "provisioned" }`. On failure, cleans up and returns 500.
- **GET /v1/admin/tenants** — Returns list of tenants with `tenant_id`, `name`, `plan`, `status` (no API keys).
- **DELETE /v1/admin/tenants/{tenant_id}** — Soft delete: sets tenant status to `deleted`, revokes all API keys. Returns 204.

## Worker Job Pattern

Workers are API-only: they never touch the database directly. They use the jobs API (same auth as CLI).

- **Claim:** GET /v1/jobs/next?job_type=…&library_id=… → 204 if no work, else job payload.
- **Complete:** POST /v1/jobs/{job_id}/complete with result body (e.g. proxy_key, thumbnail_key, width, height for proxy).
- **Fail:** POST /v1/jobs/{job_id}/fail with `{ "error_message" }`.

Lease is server-managed (worker_id generated per claim). Expired leases are reclaimed on next poll. Worker types in v1: `proxy`. Type `face` is reserved for phase 2 — do not implement.

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
