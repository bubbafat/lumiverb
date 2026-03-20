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
- `tenants` — tenant_id, name, plan, status, vision_api_url, vision_api_key, created_at
- `api_keys` — key_hash, tenant_id, name, scopes, role (`admin`|`member`), created_at
- `tenant_db_routing` — tenant_id, connection_string, region

**Tenant DB** (one per tenant, same Postgres instance):
- `libraries` — library_id, name, root_path, scan_status, created_at
- `library_path_filters` — filter_id (lpf_+ULID), library_id FK, type (include|exclude), pattern, created_at. Controls which paths are ingested per library.
- `tenant_path_filter_defaults` — default_id (tpfd_+ULID), tenant_id, type (include|exclude), pattern, created_at. Copied to new libraries at creation only.
- `assets` — asset_id, library_id, sha256, file_path, file_size, media_type, width, height, duration_ms, duration_sec, captured_at, proxy_key, proxy_sha256, thumbnail_key, thumbnail_sha256, availability, video_indexed, created_at
- `video_scenes` — scene_id, asset_id, start_ms, end_ms, rep_frame_ms, proxy_key, thumbnail_key, rep_frame_sha256, description, tags, sharpness_score, keep_reason, phash, created_at
- `video_index_chunks` — chunk_id, asset_id, chunk_index, start_ms, end_ms, status, worker_id, claimed_at, lease_expires_at, completed_at, error_message, anchor_phash, scene_start_ms, created_at
- `asset_metadata` — asset_id, exif_json, sharpness_score, face_count, ai_description, ai_description_at, embedding_vector vector(512) [nullable]
- `search_sync_queue` — asset_id, scene_id, operation, status, created_at
- `worker_jobs` — job_id, job_type, asset_id, status, worker_id, claimed_at, completed_at, error
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
3. **`get_by_library_and_rel_path()` intentionally returns trashed assets** because the scan upsert path needs to detect them to avoid a unique-constraint violation on `(library_id, rel_path)`. It is the only read method that does this. All callers must check `deleted_at` explicitly, or call a write method (`update_for_scan`, `create_or_update_for_scan_bulk`) that clears it.
4. **Scan write methods clear `deleted_at` unconditionally.** Both `update_for_scan` and the `ON CONFLICT DO UPDATE` in `create_or_update_for_scan_bulk` set `deleted_at = NULL`. A file present on disk during a scan is by definition active — this is the correct restore behaviour.
5. **`search_sync_queue.pending_count()` must include expired-processing rows**, matching the scope of `claim_batch()`. Using only `status = 'pending'` produces a misleading count when rows are stuck in `processing` after an interrupted run.

When writing queries, always use the tenant DB session, not the control plane session. The middleware resolves this from the API key before the route handler runs.

Tenant resolution runs for every request except `/health` and `/v1/admin/*`: reads `Authorization: Bearer <token>`, validates via control plane `ApiKeyRepository.get_by_plaintext`, touches `last_used_at`, looks up `TenantDbRouting` for the connection string, and stores `tenant_id` and `connection_string` in `request.state`. Use the `get_tenant_session` dependency in route handlers to obtain a tenant DB session.

## Tenant Context

- **GET /v1/tenant/context** — Tenant auth required. Returns `{ "tenant_id" }` only. Used by CLI/worker for storage path computation. Workers must not have direct DB access; they use the jobs API only.

## Tenant Maintenance API

All routes under `/v1/tenant/maintenance` require tenant auth and **tenant admin** role (`require_tenant_admin`). Maintenance mode is stored as a JSON value in `system_metadata` under the key `maintenance_mode`. When active, `GET /v1/jobs/next` returns 204 immediately so workers idle without claiming jobs.

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

## Jobs API

All under `/v1/jobs`; require tenant auth.

- **POST /v1/jobs/enqueue** — Body: `{ "job_type", "filter", "force" }`. `filter` is an AssetFilterSpec: `library_id` (required), optional `asset_id`, `path_prefix`, `path_exact`, `mtime_after`, `mtime_before`, `missing_proxy`, `missing_thumbnail`, `retry_failed`. `force` (default false): if true, cancels existing pending/claimed jobs for matching assets then enqueues. `filter.retry_failed` (default false): if true, re-enqueues only assets with failed jobs (mutually exclusive with `force`). Returns `{ "enqueued" }` (count of jobs created).
- **GET /v1/jobs/next** — Query: `job_type` (required), `library_id` (optional). Claims next pending job; returns 204 if none. On success returns `{ "job_id", "job_type", "asset_id", "rel_path", "media_type", "library_id", "root_path", "proxy_key", "thumbnail_key", "vision_model_id", "vision_api_url", "vision_api_key" }`. `vision_api_url` and `vision_api_key` come from the tenant record (control plane); workers use these to call the OpenAI-compatible vision API. For video assets, also includes `"duration_sec"` (from asset.duration_sec or duration_ms/1000). 404 if asset or library not found (job is failed server-side).
- **GET /v1/jobs/pending** — Query: `job_type` (required), `library_id` (optional). Returns `{ "pending": N }` count of pending/claimed jobs. Same filters as `/next`. Used by workers for progress display (total work remaining).
- **POST /v1/jobs/{job_id}/complete** — Body depends on job_type: **proxy** — `proxy_key`, `thumbnail_key`, `width`, `height` (or empty body to skip, e.g. video proxy deferred); **exif** — `sha256`, `exif`, `camera_make`, `camera_model`, `taken_at`, `gps_lat`, `gps_lon`; **ai_vision** — `model_id`, `model_version`, `description`, `tags`; **embed** — `embeddings`; **video-index** — no body (chunk work done via video API); **video-vision** — same as ai_vision; marks asset `video_indexed` true and enqueues search sync. Returns `{ "job_id", "status": "completed" }`. 404 if job not found, 409 if job not claimed.
- **POST /v1/jobs/{job_id}/fail** — Body: `{ "error_message" }`. Marks job failed. Returns `{ "job_id", "status": "failed" }`.
- **GET /v1/jobs** — Query: `library_id` (optional). List jobs; filter by library when provided. Returns list of `{ "job_id", "job_type", "asset_id", "status" }`.
- **GET /v1/jobs/{job_id}/status** — Returns `{ "job_id", "status", "error_message" }`. 404 if not found.
- **GET /v1/jobs/failures** — Query: `library_id` (required), `job_type` (required), `path_prefix` (optional), `limit` (optional, default 20). Returns `{ "rows": [{ "rel_path", "error_message", "failed_at" }], "total_count" }`. Most recent failed job per asset, ordered by `rel_path`. `failed_at` is ISO 8601 or null.

Valid `job_type` values: `proxy`, `exif`, `ai_vision`, `embed`, `video-index`, `video-vision`. For `video-index`, the worker claims one job per asset, then uses the video chunk API to claim and complete 30-second chunks; when all chunks are done the server enqueues a `video-vision` job for that asset.

## Pipeline API

All under `/v1/pipeline`; require tenant auth. These endpoints allow the CLI to operate the pipeline supervisor without direct database access, enabling the databases to be hosted remotely.

### Lock

One lock per tenant. The lock_id returned on acquire must be stored by the supervisor and supplied on release to prevent a crashed-then-restarted process from accidentally releasing a lock reacquired by a newer instance.

- **POST /v1/pipeline/lock/acquire** — Body: `{ "lock_timeout_minutes": 5, "force": false }`. Acquires the pipeline lock. Returns `{ "lock_id", "tenant_id" }` on success. Returns 409 with `{ "code": "lock_held", "message": "...", "details": { "hostname", "pid", "started_at" } }` if a fresh lock is held by another process and `force=false`. `force=true` overwrites any existing lock unconditionally.
- **POST /v1/pipeline/lock/heartbeat** — No body. Updates `heartbeat_at` for the tenant's lock. Returns 204. Call every ~30s from the supervisor's background thread to keep the lock fresh.
- **POST /v1/pipeline/lock/release** — Body: `{ "lock_id": "lock_..." }` (optional). Deletes the lock for this tenant. If `lock_id` is provided, the lock is only deleted when the stored `lock_id` matches — a stale `lock_id` is a no-op. Returns 204.

### Status

- **GET /v1/pipeline/status** — Query: `library_id` (optional). Returns pipeline stage counts in the same shape as `lumiverb status --output json`.
  - With `library_id`: `{ "library", "library_id", "total_assets", "workers", "stages": [{ "name", "label", "done", "inflight", "pending", "failed", "blocked" }] }`. 404 if library not found.
  - Without `library_id` (tenant-wide): `{ "workers", "libraries": [{ "library", "library_id", "total_assets", "stages": [...] }] }`.
  - Stage names: `proxy`, `exif`, `ai_vision`, `search_sync`, `embed`, `video-index`, `video-vision`, `video-preview`. Only stages with non-zero total counts are included.

## Search Sync API

All under `/v1/search-sync`; require tenant auth. These endpoints drive Quickwit indexing server-side so that CLI/worker processes do not need direct Quickwit or DB access.

- **GET /v1/search-sync/pending** — Query: `library_id` (required), `path_prefix` (optional). Returns `{ "count": N }` — number of rows in `search_sync_queue` that are pending or have an expired processing lease. Matches the scope of `process-batch`.
- **POST /v1/search-sync/process-batch** — Body: `{ "library_id", "path_prefix": null, "batch_size": 100 }`. Claims one batch from the queue using `FOR UPDATE SKIP LOCKED`, builds Quickwit documents server-side, ingests them, and marks the rows synced. Returns `{ "processed": bool, "synced": int, "skipped": int }`. `processed=false` means the queue was empty (no rows claimed). `synced` = assets ingested; `skipped` = assets with missing metadata or missing asset record. Call in a loop until `processed=false`. 404 if library not found.
- **POST /v1/search-sync/resync** — Body: `{ "library_id", "path_prefix": null }`. Re-enqueues all active (non-trashed) assets for the library into `search_sync_queue` (equivalent to `--force-resync`). Returns `{ "enqueued": N }`. 404 if library not found.

## Video chunk API

All under `/v1/video`; require tenant auth. Used by the video-index worker to process video assets in 30-second chunks (server-owned policy). No video bytes reach the server — only scene rep frame keys and metadata.

- **POST /v1/video/{asset_id}/chunks** — Body: `{ "duration_sec" }`. Initialize chunks for the asset (idempotent). Returns `{ "chunk_count", "already_initialized" }`.
- **GET /v1/video/{asset_id}/chunks/next** — Claim next pending chunk for the asset. Returns 204 if none. On success returns `{ "chunk_id", "worker_id", "chunk_index", "start_ts", "end_ts", "overlap_sec", "anchor_phash", "scene_start_ts", "video_duration_sec", "is_last" }`. Worker must send `worker_id` when completing or failing the chunk.
- **POST /v1/video/chunks/{chunk_id}/complete** — Body: `{ "worker_id", "scenes", "next_anchor_phash", "next_scene_start_ms" }`. `scenes`: list of `{ "scene_index", "start_ms", "end_ms", "rep_frame_ms", "proxy_key", "thumbnail_key", "description", "tags", "sharpness_score", "keep_reason", "phash" }`. Persists scenes, updates next chunk anchor state, marks chunk completed. When all chunks for the asset are complete, enqueues a `video-vision` job. Returns `{ "chunk_id", "scenes_saved", "all_complete" }`. 409 if chunk not owned by worker.
- **POST /v1/video/chunks/{chunk_id}/fail** — Body: `{ "worker_id", "error_message" }`. Marks chunk failed. Returns `{ "chunk_id", "status": "failed" }`. 409 if chunk not owned by worker.
- **GET /v1/video/{asset_id}/scenes** — Returns all scenes for an asset ordered by `start_ms`. Used by VideoVisionWorker. Response: `{ "scenes": [ { "scene_id", "start_ms", "end_ms", "rep_frame_ms", "thumbnail_key", "description", "tags", "sharpness_score", "keep_reason", "phash" } ] }`.
- **PATCH /v1/video/scenes/{scene_id}** — Body: `{ "model_id", "model_version", "description", "tags" }`. Updates vision results on a scene after describing its rep frame. Response: `{ "scene_id", "status": "updated" }`.
- **POST /v1/video/scenes/{scene_id}/sync** — Body: `{ "asset_id" }`. Enqueues a search sync entry for the given scene. Response: `{ "scene_id", "status": "enqueued" }`.

## Libraries API

All under `/v1/libraries`; require tenant auth (middleware).

- **POST /v1/libraries** — Body: `{ "name", "root_path", "vision_model_id" }` (`vision_model_id` optional, defaults to `""`). Name must be unique per tenant (409 if duplicate). New libraries inherit the tenant's path filter defaults at creation time (subsequent changes to defaults do not affect existing libraries). Returns `{ "library_id", "name", "root_path", "scan_status", "vision_model_id" }` (scan_status initially `"idle"`).
- **PATCH /v1/libraries/{library_id}** — Body: `{ "name", "vision_model_id" }` (both optional). Updates library name and/or vision model ID. Returns full library response.
- **GET /v1/libraries** — Query: `include_trashed` (optional, default false). Returns list of libraries with `library_id`, `name`, `root_path`, `scan_status`, `last_scan_at`, `status` (`"active"` or `"trashed"`). Trashed libraries excluded unless `include_trashed=true`.
- **DELETE /v1/libraries/{library_id}** — Soft delete: set library `status` to `"trashed"`, cancel pending/claimed worker jobs for its assets. Returns 204 on success, 404 if not found, 409 if already trashed.
- **POST /v1/libraries/empty-trash** — Hard delete all trashed libraries for this tenant (cascade: worker_jobs, search_sync_queue, asset_metadata, video_scenes, assets, scans, library_path_filters, libraries). Returns `{ "deleted": N }`.

## Library path filters API

All under `/v1/libraries/{library_id}/filters`; require tenant auth and **admin** API key. Path filters control which files are included or excluded during library ingest (scanner). Patterns use `**`-style globs (case-insensitive); `**` matches across path segments. Validation rejects patterns containing `..` or null bytes.

- **GET /v1/libraries/{library_id}/filters** — Returns `{ "includes": [{ "filter_id", "pattern", "created_at" }], "excludes": [...] }`. 404 if library not found.
- **POST /v1/libraries/{library_id}/filters** — Body: `{ "type": "include"|"exclude", "pattern": "..." }`. Creates filter. Returns 201 with `{ "filter_id", "type", "pattern", "created_at" }`. 400 if pattern invalid, 404 if library not found.
- **DELETE /v1/libraries/{library_id}/filters/{filter_id}** — Removes filter. Returns 204 on success, 404 if not found.

## Tenant filter defaults API

All under `/v1/tenant/filter-defaults`; require tenant auth and **admin** API key. Defaults are copied to each new library at creation time only.

- **GET /v1/tenant/filter-defaults** — Returns `{ "includes": [{ "default_id", "pattern", "created_at" }], "excludes": [...] }`.
- **POST /v1/tenant/filter-defaults** — Body: `{ "type": "include"|"exclude", "pattern": "..." }`. Creates default. Returns 201 with `{ "default_id", "type", "pattern", "created_at" }`. 400 if pattern invalid.
- **DELETE /v1/tenant/filter-defaults/{default_id}** — Removes default. Returns 204 on success, 404 if not found.

## Scans API

All under `/v1/scans`; require tenant auth.

- **POST /v1/scans** — Body: `{ "library_id", "status": "running|aborted|error", "root_path_override": null, "worker_id": null, "error_message": null }`. Creates scan record; if status is `running` sets library `scan_status` to `"scanning"`; if `aborted` or `error` updates library `scan_status` and `last_scan_error`. Returns `{ "scan_id" }`.
- **GET /v1/scans/running?library_id=** — Returns list of running scans: `{ "scan_id", "library_id", "started_at", "worker_id" }`.
- **POST /v1/scans/{scan_id}/batch** — Body: `{ "items": [{ "action": "skip"|"update"|"missing"|"add", ... }] }`. Process bulk scan actions: skip (touch), update (file_size, file_mtime), missing (set availability), add (insert/upsert by rel_path). Accumulates counts on scan record. Returns `{ "added", "updated", "skipped", "missing" }`.
- **POST /v1/scans/{scan_id}/complete** — Body: optional (ignored for backward compat). Marks assets not seen in this scan as missing, completes scan, updates library `scan_status` and `last_scan_at`. Counts are accumulated server-side via batch endpoint. Returns `{ "scan_id", "files_discovered", "files_added", "files_updated", "files_skipped", "files_missing", "status" }`.
- **POST /v1/scans/{scan_id}/abort** — Body: `{ "error_message": null }`. Aborts scan, updates library `scan_status` to `"error"` or `"aborted"`. Returns `{ "scan_id", "status" }`.

## Assets API

All under `/v1/assets`; require tenant auth. List/get endpoints return only active (non-trashed) assets.

- **GET /v1/assets** — Query: `library_id` (optional). List active assets; filter by library when provided. Returns list of `{ "asset_id", "library_id", "rel_path", "media_type", "status", "proxy_key", "thumbnail_key", "width", "height" }`.
- **GET /v1/assets/page** — Query: `library_id` (required), `after` (cursor), `limit` (default 500, max 500). Keyset-paginated active assets for bulk reconciliation. Returns list of `{ "asset_id", "rel_path", "file_size", "file_mtime", "sha256", "media_type" }`. Returns 204 if no results (end of pages).
- **GET /v1/assets/{asset_id}** — Return single asset. 404 if not found or trashed.
- **DELETE /v1/assets/{asset_id}** — Soft-delete (trash) a single asset. Sets `deleted_at`. Returns 204 on success, 404 if not found or already trashed. Quickwit delete is best-effort (log on failure).
- **DELETE /v1/assets** — Body: `{ "asset_ids": ["ast_...", ...] }`. Soft-delete multiple assets. Returns `{ "trashed": [...], "not_found": [...] }`. Quickwit delete is best-effort.
- **POST /v1/assets/{asset_id}/restore** — Restore a trashed asset (clear `deleted_at`). Returns 204 on success, 404 if not found or not trashed.
- **POST /v1/assets/{asset_id}/thumbnail-key** — Body: `{ "thumbnail_key" }`. Records a thumbnail_key on the asset. Used by VideoIndexWorker after extracting the first frame of a video. Returns `{ "asset_id", "thumbnail_key" }`.
- **POST /v1/assets/{asset_id}/artifacts/{artifact_type}** — Multipart file upload. `artifact_type` must be one of: `proxy`, `thumbnail`, `video_preview`. Form fields: `file` (binary, required), `width` (int, optional, images only), `height` (int, optional, images only). Streams the upload to disk in 64 KB chunks, computes SHA-256 incrementally, and atomic-renames into place. Updates DB after file is safely on disk. Returns `{ "key", "sha256" }`. Errors: 400 invalid type, 404 asset not found or trashed, 413 file too large. Does NOT advance `asset.status` — that remains the job-complete path's responsibility.
- **POST /v1/assets/upsert** — Legacy single-file upsert. Prefer POST /v1/scans/{scan_id}/batch for bulk operations. Body: `{ "library_id", "rel_path", "file_size", "file_mtime" (ISO8601), "media_type", "scan_id", "force": false }`. Upserts by `(library_id, rel_path)`. Returns `{ "action": "added|updated|skipped" }`.

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

## Worker Job Pattern

Workers are API-only: they never touch the database directly. They use the jobs API (same auth as CLI).

- **Claim:** GET /v1/jobs/next?job_type=…&library_id=… → 204 if no work, else job payload.
- **Complete:** POST /v1/jobs/{job_id}/complete with result body. Per job_type: **proxy** — `proxy_key`, `thumbnail_key`, `width`, `height`, `proxy_sha256` (optional), `thumbnail_sha256` (optional) — or empty body to skip (e.g. video proxy deferred); **exif** — `sha256`, `exif`, `camera_make`, `camera_model`, `taken_at`, `gps_lat`, `gps_lon`; **ai_vision** — `model_id`, `model_version`, `description`, `tags`; **embed** — `embeddings`: list of `{ "model_id", "model_version", "vector" }` (CLIP only — one entry per job); **video-index** — no body (worker uses video chunk API, then calls complete when all chunks done); **video-vision** — same as ai_vision; sets asset `video_indexed` and enqueues search sync.
- **Fail:** POST /v1/jobs/{job_id}/fail with `{ "error_message" }`.

Lease is server-managed (worker_id generated per claim). Expired leases are reclaimed on next poll. Worker types: `proxy`, `exif`, `ai_vision`, `embed`, `video-index`, `video-vision`. Type `face` is reserved for phase 2 — do not implement.

## SHA256 Deduplication

When an asset is submitted via POST /v1/assets:
1. Check if sha256 already exists in tenant DB
2. If yes — return existing asset_id with 200 (not 201), do not re-store
3. If no — create new asset record, store proxy/thumbnail, enqueue jobs

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

- Do not add user authentication (login/password/sessions) — that is Phase 5
- Do not add webhooks — that is future work
- Do not add multi-library search in v1 — search is per-library
- Do not store or serve source files
- Do not add rate limiting in v1 — that is future work
- Do not populate or query `embedding_vector`, `faces`, `people`, or `face_person_matches` — phase 2 only
- Do not implement face clustering or identity assignment — phase 2 only
