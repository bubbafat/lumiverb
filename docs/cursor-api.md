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
- All routes require `Authorization: Bearer {token}` header (token is a JWT for web sessions or an API key for CLI/automation)
- Tenant context is always derived from the token (JWT claims or API key lookup) — never passed as a URL param or body field
- Pagination is cursor-based, never offset-based
- File uploads are multipart form data
- File serving uses signed URLs (cloud) or direct proxy (self-hosted) — never expose raw object storage URLs
- Error responses always use this envelope: `{"error": {"code": "...", "message": "...", "details": {...}}}`
- OpenAPI spec is generated automatically from route definitions — keep docstrings accurate

## Database Model

Two-layer Postgres architecture:

**Control plane DB** (shared, tiny):
- `tenants` — tenant_id, name, plan, status, vision_api_url, vision_api_key, created_at
- `api_keys` — key_hash, tenant_id, name, scopes, role (`admin`), created_at
- `users` — user_id, tenant_id, email, password_hash, role (`admin`|`editor`|`viewer`), created_at, last_login_at
- `password_reset_tokens` — token_hash, user_id, expires_at, used_at
- `tenant_db_routing` — tenant_id, connection_string, region

**Tenant DB** (one per tenant, same Postgres instance):
- `libraries` — library_id, name, root_path, scan_status, revision (int, bumped on asset changes), created_at
- `library_path_filters` — filter_id (lpf_+ULID), library_id FK, type (include|exclude), pattern, created_at. Controls which paths are ingested per library.
- `tenant_path_filter_defaults` — default_id (tpfd_+ULID), tenant_id, type (include|exclude), pattern, created_at. Tenant defaults and library filters are merged at evaluation time (see filter evaluation rules below).
- `assets` — asset_id, library_id, sha256, file_path, file_size, media_type, width, height, duration_sec, proxy_key, proxy_sha256, thumbnail_key, thumbnail_sha256, availability, video_indexed, status, deleted_at, created_at
- `video_scenes` — scene_id, asset_id, start_ms, end_ms, rep_frame_ms, proxy_key, thumbnail_key, rep_frame_sha256, description, tags, sharpness_score, keep_reason, phash, created_at
- `video_index_chunks` — chunk_id, asset_id, chunk_index, start_ms, end_ms, status, worker_id, claimed_at, lease_expires_at, completed_at, error_message, anchor_phash, scene_start_ms, created_at
- `asset_metadata` — asset_id, exif_json, sharpness_score, face_count, ai_description, ai_description_at, embedding_vector vector(512) [nullable]
- `search_sync_queue` — asset_id, scene_id, operation, status, created_at
- `system_metadata` — key, value
- `faces` — face_id, asset_id, bounding_box_json, embedding_vector vector(512), detection_confidence, created_at [phase 2, empty until then]
- `people` — person_id, display_name, created_by_user, created_at [phase 2]
- `face_person_matches` — face_id, person_id, confidence, confirmed bool, confirmed_at [phase 2]

pgvector extension is enabled at provisioning time. The `vector(512)` columns exist from day one but are nullable and unpopulated until phase 2 workers run. Do not query these columns in phase 1 code.

### Soft delete — always use `active_assets`

Assets are soft-deleted via `deleted_at` (set by trash operations). The `active_assets` view is defined as:

```sql
SELECT * FROM assets WHERE deleted_at IS NULL
```

**Non-negotiable rules — enforced by tests in `tests/test_soft_delete_invariants.py`:**

1. **Never query `FROM assets` directly** in any read path that should return live assets. Always use `FROM active_assets` in raw SQL, or add `.where(Asset.deleted_at.is_(None))` in ORM queries.
2. **`AssetRepository.get_by_id()` returns `None` for trashed assets.** Do not use `session.get(Asset, id)` as a substitute — it bypasses the filter.
3. **`get_by_library_and_rel_path()` intentionally returns trashed assets** because the ingest upsert path needs to detect them to avoid a unique-constraint violation on `(library_id, rel_path)`. It is the only read method that does this. Ingest clears `deleted_at` on update — a file present on disk is by definition active.
4. **`search_sync_queue.pending_count()` must include expired-processing rows**, matching the scope of `claim_batch()`. Using only `status = 'pending'` produces a misleading count when rows are stuck in `processing` after an interrupted run.

When writing queries, always use the tenant DB session, not the control plane session. The middleware resolves this from the bearer token before the route handler runs.

Tenant resolution runs for every request except `/health`, `/v1/admin/*`, and `/v1/auth/*`: reads `Authorization: Bearer <token>`, attempts JWT decode first (signature + expiry + required claims: `sub`, `tenant_id`, `role`), falls through to API key lookup on JWT failure, looks up `TenantDbRouting` for the connection string, and stores `tenant_id`, `connection_string`, `user_id`, `key_id`, `role`, and `is_public_request` in `request.state`. Use the `get_tenant_session` dependency in route handlers to obtain a tenant DB session.

## Tenant Context

- **GET /v1/tenant/context** — Tenant auth required. Returns `{ "tenant_id", "vision_api_url", "vision_api_key" }`. Used by CLI/worker for storage path computation and vision config fallback. Workers must not have direct DB access; they use the jobs API only. Client-side `vision_api_url`/`vision_api_key` in `~/.lumiverb/config.json` override the tenant values (hybrid config).

## Current User

- **GET /v1/me** — Returns `{ user_id, email, role }` for the authenticated user. For JWT auth, looks up email from the users table. For API key auth, `user_id` and `email` are null; only `role` is returned.

## API Keys

All routes under `/v1/keys` require tenant auth and **editor or admin** role.

- **GET /v1/keys** — List all non-revoked keys for the current tenant. Requires editor or admin. Never includes plaintext.
- **POST /v1/keys** — Create a new key. Body: `{ label }`. The key inherits the caller's role (body cannot override it). Returns `{ key_id, label, role, plaintext, created_at }`. The `plaintext` value is shown exactly once.
- **DELETE /v1/keys/{key_id}** — Revoke a key. Returns 204. A key cannot revoke itself (409). The last admin key cannot be revoked (409).

## Tenant Maintenance API

All routes under `/v1/tenant/maintenance` require tenant auth and **tenant admin** role (`require_tenant_admin`). Maintenance mode is stored as a JSON value in `system_metadata` under the key `maintenance_mode`. Used to pause operations during upgrades.

- **GET /v1/tenant/maintenance/status** — Returns `{ active, message, started_at }`.
- **POST /v1/tenant/maintenance/start** — Body: `{ "message": "..." }`. Enables maintenance mode. Returns `{ active: true, message, started_at }`.
- **POST /v1/tenant/maintenance/end** — No body. Clears maintenance mode. Returns `{ active: false }`.

## Tenant Upgrade API

All routes under `/v1/tenant/upgrade` require tenant auth and **tenant admin** role (`require_tenant_admin`). These endpoints run idempotent, tenant-scoped upgrade steps (schema migrations and/or data backfills) in a fixed order.

- **GET /v1/tenant/upgrade/status** — Returns `{ has_work, steps_total, done_steps, completed_steps, pending_steps, skipped_steps, failed_steps, next_pending_step_id, remaining_pending_step_ids, steps }` where each step includes `step_id`, `version`, `display_name`, and `status` (`pending|skipped|completed|failed`).
- **POST /v1/tenant/upgrade/execute** — Body: `{ "max_steps": 1, "step_id": null, "force": false }`.
  - With `step_id=null`, runs up to `max_steps` pending steps in order.
  - With `step_id` set, runs only that step if it is pending.
  - Without `force`, the server refuses to run a targeted step when any preceding step is still `pending` or `failed`.
  - Returns `{ ran_steps, steps_completed_now, has_work_after, remaining_pending_step_ids, total_steps, done_steps, completed_steps, failed_steps }`.

## Search Sync API

All under `/v1/search-sync`; require tenant auth. These endpoints drive Quickwit indexing server-side so that CLI/worker processes do not need direct Quickwit or DB access.

- **GET /v1/search-sync/pending** — Query: `library_id` (required), `path_prefix` (optional). Returns `{ "count": N }` — number of rows in `search_sync_queue` that are pending or have an expired processing lease. Matches the scope of `process-batch`.
- **POST /v1/search-sync/process-batch** — Body: `{ "library_id", "path_prefix": null, "batch_size": 100 }`. Claims one batch from the queue using `FOR UPDATE SKIP LOCKED`, builds Quickwit documents server-side, ingests them, and marks the rows synced. Returns `{ "processed": bool, "synced": int, "skipped": int }`. `processed=false` means the queue was empty (no rows claimed). `synced` = assets ingested; `skipped` = assets with missing metadata or missing asset record. Call in a loop until `processed=false`. 404 if library not found.
- **POST /v1/search-sync/resync** — Body: `{ "library_id", "path_prefix": null }`. Re-enqueues all active (non-trashed) assets for the library into `search_sync_queue` (equivalent to `--force-resync`). Returns `{ "enqueued": N }`. 404 if library not found.

## Video chunk API

All under `/v1/video`; require tenant auth. Used by the video-index worker to process video assets in 30-second chunks (server-owned policy). No video bytes reach the server — only scene rep frame keys and metadata.

- **POST /v1/video/{asset_id}/chunks** — Body: `{ "duration_sec" }`. Initialize chunks for the asset (idempotent). Returns `{ "chunk_count", "already_initialized" }`.
- **GET /v1/video/{asset_id}/chunks/next** — Claim next pending chunk for the asset. Returns 204 if none. On success returns `{ "chunk_id", "worker_id", "chunk_index", "start_ts", "end_ts", "overlap_sec", "anchor_phash", "scene_start_ts", "video_duration_sec", "is_last" }`. Worker must send `worker_id` when completing or failing the chunk.
- **POST /v1/video/chunks/{chunk_id}/complete** — Body: `{ "worker_id", "scenes", "next_anchor_phash", "next_scene_start_ms" }`. `scenes`: list of `{ "scene_index", "start_ms", "end_ms", "rep_frame_ms", "proxy_key", "thumbnail_key", "description", "tags", "sharpness_score", "keep_reason", "phash" }`. Persists scenes, updates next chunk anchor state, marks chunk completed. When all chunks for the asset are complete, marks the asset as `video_indexed`. Returns `{ "chunk_id", "scenes_saved", "all_complete" }`. 409 if chunk not owned by worker.
- **POST /v1/video/chunks/{chunk_id}/fail** — Body: `{ "worker_id", "error_message" }`. Marks chunk failed. Returns `{ "chunk_id", "status": "failed" }`. 409 if chunk not owned by worker.
- **GET /v1/video/{asset_id}/scenes** — Returns all scenes for an asset ordered by `start_ms`. Used by VideoVisionWorker. Response: `{ "scenes": [ { "scene_id", "start_ms", "end_ms", "rep_frame_ms", "thumbnail_key", "description", "tags", "sharpness_score", "keep_reason", "phash" } ] }`.
- **PATCH /v1/video/scenes/{scene_id}** — Body: `{ "model_id", "model_version", "description", "tags" }`. Updates vision results on a scene after describing its rep frame. Response: `{ "scene_id", "status": "updated" }`.
- **POST /v1/video/scenes/{scene_id}/sync** — Body: `{ "asset_id" }`. Enqueues a search sync entry for the given scene. Response: `{ "scene_id", "status": "enqueued" }`.

## Libraries API

All under `/v1/libraries`; require tenant auth (middleware).

- **POST /v1/libraries** — Body: `{ "name", "root_path", "vision_model_id" }` (`vision_model_id` optional, defaults to `""`). Name must be unique per tenant (409 if duplicate). New libraries inherit the tenant's path filter defaults at creation time (subsequent changes to defaults do not affect existing libraries). Returns `{ "library_id", "name", "root_path", "scan_status", "vision_model_id", "is_public" }` (scan_status initially `"idle"`, is_public initially `false`).
- **PATCH /v1/libraries/{library_id}** — Body: `{ "name", "vision_model_id", "is_public" }` (all optional). Updates library name, vision model ID, and/or public visibility. Setting `is_public: true` inserts a row in the `public_libraries` control plane table, enabling unauthenticated access (Phase 3). Setting `is_public: false` removes it. Returns full library response including `is_public`.
- **GET /v1/libraries** — Query: `include_trashed` (optional, default false). Returns list of libraries with `library_id`, `name`, `root_path`, `scan_status`, `last_scan_at`, `status` (`"active"` or `"trashed"`), `is_public`. Trashed libraries excluded unless `include_trashed=true`.
- **DELETE /v1/libraries/{library_id}** — Soft delete: set library `status` to `"trashed"`, soft-delete all assets (`deleted_at` set). If library was public, removes its `public_libraries` control plane row. Returns 204 on success, 404 if not found, 409 if already trashed.
- **POST /v1/libraries/empty-trash** — Hard delete all trashed libraries for this tenant (cascade: asset_metadata, asset_embeddings, video_scenes, video_index_chunks, assets, library_path_filters, libraries). Removes `public_libraries` control plane rows for any trashed libraries that were public. Returns `{ "deleted": N }`.
- **GET /v1/libraries/{library_id}/revision** — Lightweight polling endpoint. Returns `{ "library_id", "revision", "asset_count" }`. The `revision` counter increments atomically on asset create/update (ingest) and vision metadata submission. UI clients poll this every 10 seconds and use `revision` in query keys to trigger cache invalidation when data changes.

## Library path filters API

All under `/v1/libraries/{library_id}/filters`; require tenant auth and **admin** API key. Path filters control which files are included or excluded during library ingest (scanner). Patterns use `**`-style globs (case-insensitive); `**` matches across path segments. Validation rejects patterns containing `..` or null bytes.

- **GET /v1/libraries/{library_id}/filters** — Returns `{ "includes": [{ "filter_id", "pattern", "created_at" }], "excludes": [...] }`. 404 if library not found.
- **POST /v1/libraries/{library_id}/filters** — Body: `{ "type": "include"|"exclude", "pattern": "..." }`. Creates filter. Returns 201 with `{ "filter_id", "type", "pattern", "created_at" }`. 400 if pattern invalid, 404 if library not found.
- **DELETE /v1/libraries/{library_id}/filters/{filter_id}** — Removes filter. Returns 204 on success, 404 if not found.

## Tenant filter defaults API

All under `/v1/tenant/filter-defaults`; require tenant auth and **editor** role. Tenant defaults apply dynamically to all libraries via merged evaluation (see below).

- **GET /v1/tenant/filter-defaults** — Returns `{ "includes": [{ "default_id", "pattern", "created_at" }], "excludes": [...] }`.
- **POST /v1/tenant/filter-defaults** — Body: `{ "type": "include"|"exclude", "pattern": "..." }`. Creates default. Returns 201 with `{ "default_id", "type", "pattern", "created_at" }`. 400 if pattern invalid.
- **DELETE /v1/tenant/filter-defaults/{default_id}** — Removes default. Returns 204 on success, 404 if not found.

### Merged filter evaluation

Tenant defaults and library filters are merged at evaluation time with "library wins" priority:

1. **Library exclude** matches → **BLOCKED** (absolute, highest priority)
2. **Library include** matches → **ALLOWED** (overrides tenant restrictions)
3. **Tenant exclude** matches → **BLOCKED**
4. **Tenant includes exist** but path doesn't match any → **BLOCKED**
5. **Default** → **ALLOWED**

This is enforced both client-side (during filesystem walk for efficiency) and server-side (POST /v1/ingest returns 422 for filtered paths).

## Assets API

All under `/v1/assets`; require tenant auth. List/get endpoints return only active (non-trashed) assets.

- **GET /v1/assets** — Query: `library_id` (optional). List active assets; filter by library when provided. Returns list of `{ "asset_id", "library_id", "rel_path", "media_type", "status", "proxy_key", "thumbnail_key", "width", "height" }`.
- **GET /v1/assets/page** — Query: `library_id` (required), `after` (cursor), `limit` (default 500, max 500), `missing_vision` (optional bool, filters to assets without AI metadata). Keyset-paginated active assets for bulk reconciliation. Returns list of `{ "asset_id", "rel_path", "file_size", "file_mtime", "sha256", "media_type" }`. Returns 204 if no results (end of pages).
- **GET /v1/assets/{asset_id}** — Return single asset. 404 if not found or trashed.
- **DELETE /v1/assets/{asset_id}** — Soft-delete (trash) a single asset. Sets `deleted_at`. Returns 204 on success, 404 if not found or already trashed. Quickwit delete is best-effort (log on failure).
- **DELETE /v1/assets** — Body: `{ "asset_ids": ["ast_...", ...] }`. Soft-delete multiple assets. Returns `{ "trashed": [...], "not_found": [...] }`. Quickwit delete is best-effort.
- **POST /v1/assets/{asset_id}/restore** — Restore a trashed asset (clear `deleted_at`). Returns 204 on success, 404 if not found or not trashed.
- **POST /v1/assets/{asset_id}/thumbnail-key** — Body: `{ "thumbnail_key" }`. Records a thumbnail_key on the asset. Used by VideoIndexWorker after extracting the first frame of a video. Returns `{ "asset_id", "thumbnail_key" }`.
- **POST /v1/assets/{asset_id}/artifacts/{artifact_type}** — Multipart file upload. `artifact_type` must be one of: `proxy`, `thumbnail`, `video_preview`, `scene_rep`. Form fields: `file` (binary, required), `width` (int, optional, images only), `height` (int, optional, images only), `rep_frame_ms` (int, required for `scene_rep`, ignored for other types). Streams the upload to disk in 64 KB chunks, computes SHA-256 incrementally, and atomic-renames into place. Updates DB after file is safely on disk. Returns `{ "key", "sha256" }`. Errors: 400 invalid type or missing `rep_frame_ms` for `scene_rep`, 404 asset not found or trashed, 413 file too large. Does NOT advance `asset.status` — that remains the job-complete path's responsibility.
- **POST /v1/assets/upsert** — Single-file upsert. Body: `{ "library_id", "rel_path", "file_size", "file_mtime" (ISO8601), "media_type", "force": false }`. Upserts by `(library_id, rel_path)`. Returns `{ "action": "added|updated|skipped" }`.

## Ingest API

Atomic ingest: create + populate assets in one request. The server normalizes the proxy (WebP, 2048px max), generates a thumbnail (WebP, 512px), and stores all provided metadata atomically. If the client sends a WebP proxy already within size limits, the server stores it as-is (no re-encoding).

- **POST /v1/ingest** — Multipart form. Creates asset record AND ingests proxy + metadata atomically. The asset only appears on the server once fully populated. If an asset with the same `(library_id, rel_path)` already exists, it is updated (idempotent). Required fields: `proxy` (file), `library_id`, `rel_path`, `file_size`. Optional: `file_mtime` (ISO8601), `media_type` (default `image/jpeg`), `width`/`height` (source dimensions), `exif` (JSON), `vision` (JSON), `embeddings` (JSON array). Returns `{ "asset_id", "proxy_key", "proxy_sha256", "thumbnail_key", "thumbnail_sha256", "status", "width", "height", "created" }`. Enforces library path filters: 422 if `rel_path` is excluded.
- **POST /v1/assets/{asset_id}/ingest** — Ingest into an existing asset record. Same proxy + metadata fields minus `library_id`/`rel_path`/`file_size`.

## Trash API

- **DELETE /v1/trash/empty** — Permanently delete trashed assets. Requires admin API key. Body: `{ "asset_ids": ["ast_..."] (optional), "trashed_before": "2026-01-01T00:00:00Z" (optional) }`. If both omitted, deletes all trashed. Scope: intersection when both provided. Deletes DB rows in FK-safe order, then best-effort proxy/thumbnail file removal and Quickwit delete. Returns `{ "deleted": N }`.

## Admin API

Admin routes live under `/v1/admin` and require `Authorization: Bearer {ADMIN_KEY}` (not tenant API keys). If `ADMIN_KEY` is not set, admin routes return 500.

- **POST /v1/admin/tenants** — Body: `{ "name", "plan": "free|pro|enterprise", "email", "vision_api_url", "vision_api_key" }`. Creates tenant, provisions tenant DB (pgvector + Alembic), creates routing row, creates default API key. Returns `{ "tenant_id", "api_key", "database": "provisioned" }`. On failure, cleans up and returns 500.
- **GET /v1/admin/tenants** — Returns list of tenants with `tenant_id`, `name`, `plan`, `status` (no API keys, no vision credentials).
- **PATCH /v1/admin/tenants/{tenant_id}** — Body: `{ "vision_api_url", "vision_api_key" }` (both optional; only provided fields are updated). Updates tenant vision API config. Returns `{ "tenant_id", "vision_api_url" }`. 404 if tenant not found or deleted.
- **POST /v1/admin/tenants/{tenant_id}/keys** — Body: `{ "name" }` (human-readable label). Creates new API key for tenant. Returns `{ "api_key", "name", "tenant_id" }`. Raw key returned once and never stored. 404 if tenant does not exist or is soft-deleted.
- **GET /v1/admin/tenants/{tenant_id}/keys** — Returns list of key metadata: `name`, `tenant_id`, `created_at` (never raw keys). 404 if tenant does not exist or is soft-deleted.
- **DELETE /v1/admin/tenants/{tenant_id}** — Soft delete: sets tenant status to `deleted`, revokes all API keys. Returns 204.

## SHA256 Deduplication

When an asset is submitted via POST /v1/ingest:
1. Check if `(library_id, rel_path)` already exists in tenant DB
2. If yes — update existing asset record with new metadata
3. If no — create new asset record, store proxy/thumbnail/metadata atomically

## Search

Search is BM25 via Quickwit. The API queries Quickwit then enriches results with Postgres metadata. Never query Postgres for full-text search.

- **GET /v1/search** — Query: `library_id` (required), `q` (required, 1–500 chars), `limit` (default 20, max 100), `offset` (default 0). Asset-level BM25 search. Tries Quickwit first; falls back to Postgres ILIKE when Quickwit disabled or errors and fallback enabled. Returns `{ "query", "hits", "total", "source" }`.
- **GET /v1/search/scenes** — Query: `library_id` (required), `q` (required, 1–500 chars), `limit` (default 20, max 100), `offset` (default 0). Scene-level BM25 search via Quickwit. Returns `{ "query", "hits": [ { "scene_id", "asset_id", "rel_path", "start_ms", "end_ms", "rep_frame_ms", "thumbnail_key", "duration_sec", "description", "tags", "score", "source" } ], "total", "source" }`. No Postgres fallback. Returns empty hits if Quickwit is disabled.

**Similarity (GET /v1/similar):** Find visually similar assets by vector similarity (pgvector). Query params: `asset_id` (required), `library_id` (required), `limit` (default 20, max 100), `offset` (default 0). Optional scope filters: `from_ts`, `to_ts` (Unix timestamp seconds, inclusive capture-time range; uses `assets.taken_at`); `asset_types` (comma-separated: `image`, `video`; restricts by `media_type` prefix, e.g. `image` matches `image/jpeg`); `camera_make` and `camera_model` (repeatable; pairs by index, OR across pairs — e.g. `camera_make=Canon&camera_model=EOS&camera_make=Nikon&camera_model=Z9`). Returns `{ source_asset_id, hits, total, embedding_available }`. Excludes the source asset from results. If both `from_ts` and `to_ts` are set, `from_ts` must be ≤ `to_ts` (422 otherwise).

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
CONTROL_PLANE_DATABASE_URL=postgresql://...
TENANT_DATABASE_URL_TEMPLATE=postgresql://.../{tenant_id}
STORAGE_PROVIDER=local|gcs|s3
DATA_DIR=./data
QUICKWIT_URL=http://quickwit:7280
ADMIN_KEY=...
API_SECRET_KEY=...
APP_ENV=development|production
```

Vision API config (`vision_api_url`, `vision_api_key`) is stored per-tenant in the control plane DB, not in environment variables. Set via `PATCH /v1/admin/tenants/{tenant_id}` or `lumiverb tenant set-vision`.

## What Not to Build

- Do not add webhooks — that is future work
- Do not add multi-library search in v1 — search is per-library
- Do not store or serve source files
- Do not add rate limiting in v1 — that is future work
- Do not populate or query `embedding_vector`, `faces`, `people`, or `face_person_matches` — phase 2 only
- Do not implement face clustering or identity assignment — phase 2 only
