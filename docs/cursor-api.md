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
- `tenants` — tenant_id, name, plan, status, vision_api_url, vision_api_key, vision_model_id, created_at
- `api_keys` — key_id, key_hash, tenant_id, name, label, scopes, role (`admin`|`editor`|`viewer`), created_at, last_used_at, revoked_at
- `users` — user_id, tenant_id, email, password_hash, role (`admin`|`editor`|`viewer`), created_at, last_login_at
- `password_reset_tokens` — token_hash, user_id, expires_at, used_at
- `public_libraries` — library_id, tenant_id, connection_string, created_at
- `public_projects` — project_id, tenant_id, connection_string, created_at
- `revoked_tokens` — jti (PK), revoked_at. Tracks revoked JWTs for server-side logout/refresh revocation. Cleaned up by upkeep sweep (entries older than 8 days).
- `tenant_db_routing` — tenant_id, connection_string, region

**Tenant DB** (one per tenant, same Postgres instance):
- `libraries` — library_id, name, root_path, status, last_scan_at (updated on each ingest via bump_revision), is_public, revision (int, bumped on asset ingest), created_at, updated_at
- `library_path_filters` — filter_id (lpf_+ULID), library_id FK, type (include|exclude), pattern, created_at. Controls which paths are ingested per library.
- `tenant_path_filter_defaults` — default_id (tpfd_+ULID), tenant_id, type (include|exclude), pattern, created_at. Tenant defaults and library filters are merged at evaluation time (see filter evaluation rules below).
- `assets` — asset_id, library_id, rel_path, sha256, file_size, file_mtime, media_type, width, height, duration_sec, proxy_key, proxy_sha256, thumbnail_key, thumbnail_sha256, exif (JSON), exif_extracted_at, camera_make, camera_model, taken_at, gps_lat, gps_lon, iso, exposure_time_us, aperture, focal_length, focal_length_35mm, lens_model, flash_fired, orientation, availability, status, video_indexed, video_preview_key, error_message, created_at, updated_at, deleted_at, search_synced_at
- `video_scenes` — scene_id, asset_id, scene_index, start_ms, end_ms, rep_frame_ms, proxy_key, thumbnail_key, rep_frame_sha256, description, tags (JSONB), sharpness_score, keep_reason, phash, created_at, search_synced_at
- `video_index_chunks` — chunk_id, asset_id, chunk_index, start_ms, end_ms, status, worker_id (CLI-generated session ID), claimed_at, lease_expires_at, completed_at, error_message, anchor_phash, scene_start_ms, created_at
- `asset_metadata` — metadata_id, asset_id, model_id, model_version, generated_at, data (JSONB)
- `asset_embeddings` — embedding_id, asset_id, model_id, model_version, embedding_vector vector(512), created_at
- `asset_ratings` — user_id (text), asset_id FK (ON DELETE CASCADE), favorite (bool, default false), stars (int 0-5, default 0), color (text, nullable; red|orange|yellow|green|blue|purple), updated_at. PK: (user_id, asset_id). User-scoped ratings — each user has their own independent ratings per asset.
- `saved_views` — view_id (sv_+ULID), name, query_params (URL query string), icon (nullable), owner_user_id, position (int), created_at, updated_at. User-scoped bookmarked filter presets. Navigates to `/browse?{query_params}`.
- `system_metadata` — key, value, updated_at
- `faces` — face_id, asset_id, bounding_box_json, embedding_vector vector(512), detection_confidence, detection_model, detection_model_version, created_at. Populated by CLI face detection (InsightFace). HNSW index on embedding_vector for future clustering.
- `people` — person_id, display_name, created_by_user, centroid_vector vector(512), confirmation_count, representative_face_id FK, created_at. Populated via People API and cluster management UI.
- `face_person_matches` — face_id, person_id, confidence, confirmed bool, confirmed_at. Populated when faces are assigned to people. Unique constraint on face_id (one person per face).

pgvector extension is enabled at provisioning time. Used for asset embeddings (CLIP), face embeddings (ArcFace), and similarity search.

### Soft delete — always use `active_assets`

Assets are soft-deleted via `deleted_at` (set by trash operations). The `active_assets` view is defined as:

```sql
SELECT * FROM assets WHERE deleted_at IS NULL
```

**Non-negotiable rules — enforced by tests in `tests/test_soft_delete_invariants.py`:**

1. **Never query `FROM assets` directly** in any read path that should return live assets. Always use `FROM active_assets` in raw SQL, or add `.where(Asset.deleted_at.is_(None))` in ORM queries.
2. **`AssetRepository.get_by_id()` returns `None` for trashed assets.** Do not use `session.get(Asset, id)` as a substitute — it bypasses the filter.
3. **`get_by_library_and_rel_path()` intentionally returns trashed assets** because the ingest upsert path needs to detect them to avoid a unique-constraint violation on `(library_id, rel_path)`. It is the only read method that does this. Ingest clears `deleted_at` on update — a file present on disk is by definition active.
When writing queries, always use the tenant DB session, not the control plane session. The middleware resolves this from the bearer token before the route handler runs.

Tenant resolution runs for every request except `/health`, `/v1/admin/*`, `/v1/auth/*`, and `/v1/upkeep*`: reads `Authorization: Bearer <token>`, attempts JWT decode first (signature + expiry + required claims: `sub`, `tenant_id`, `role`; checks `jti` against `revoked_tokens` table), falls through to API key lookup on JWT failure, looks up `TenantDbRouting` for the connection string, and stores `tenant_id`, `connection_string`, `user_id`, `key_id`, `role`, and `is_public_request` in `request.state`. Use the `get_tenant_session` dependency in route handlers to obtain a tenant DB session.

All API responses include security headers: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Content-Security-Policy` (script-src 'self', frame-ancestors 'none'), `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy` (camera/microphone/geolocation denied).

## Tenant Context

- **GET /v1/tenant/context** — Tenant auth required. Returns `{ "tenant_id", "vision_api_url", "vision_api_key", "vision_model_id" }`. Used by CLI for vision config fallback. Client-side `vision_api_url`/`vision_api_key` in `~/.lumiverb/config.json` override the tenant values (hybrid config).

## Auth API

Routes under `/v1/auth`; no tenant auth required (these establish auth).

- **POST /v1/auth/login** — Body: `{ "email", "password" }`. Returns `{ "access_token", "token_type": "bearer", "expires_in" }`. JWT contains `sub` (user_id), `tenant_id`, `role`, `jti` (unique token ID), `iat`, `exp` (1 hour), `refresh_exp` (7 days). Rate limited: 5 requests/min per IP.
- **POST /v1/auth/refresh** — Requires `Authorization: Bearer {token}` (may be expired). Issues a fresh JWT if the token is within its 7-day refresh window and not revoked. Revokes the old jti. Returns `{ "access_token", "token_type", "expires_in" }`. The frontend calls this transparently on 401.
- **POST /v1/auth/forgot-password** — Body: `{ "email" }`. Sends password reset email if SMTP configured. Always returns 204 (no user enumeration). Rate limited: 3 requests/min per IP.
- **POST /v1/auth/reset-password** — Body: `{ "token", "password" }`. Resets password using a reset token. Returns 204. Rate limited: 5 requests/min per IP.
- **POST /v1/auth/logout** — Revokes the JWT server-side (inserts jti into `revoked_tokens`). The token cannot be used or refreshed after logout. Returns 204.

## Users API

All routes under `/v1/users`; require tenant auth and **admin** role.

- **GET /v1/users** — List all users for the current tenant. Returns list of `{ "user_id", "email", "role", "created_at", "last_login_at" }`.
- **POST /v1/users** — Body: `{ "email", "password", "role": "viewer" }`. Creates user. Returns 201 with user details.
- **PATCH /v1/users/{user_id}** — Body: `{ "role" }`. Updates user role. Enforces last-admin invariant (cannot demote the last admin). Returns updated user.
- **DELETE /v1/users/{user_id}** — Removes user. Enforces last-admin invariant and blocks self-deletion. Returns 204.

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

## Upkeep API

All under `/v1/upkeep`. Periodic server-side maintenance tasks. Search sync uses timestamp-based tracking (`search_synced_at` on assets and video_scenes) instead of a queue table.

- **POST /v1/upkeep** — Runs all periodic upkeep tasks: search sync, face propagation (auto-assign untagged faces to known people by centroid proximity), and revoked token cleanup (purges entries older than 8 days). Returns `{ "search_sync": { "synced", "failed", "scenes_synced", "scenes_failed" }, "face_propagate": { "assigned", "scanned" } }`.
- **POST /v1/upkeep/search-sync** — Run search sync sweep only. Finds assets/scenes where `search_synced_at` is null or older than the last update, builds Quickwit documents, and ingests them. Returns `{ "synced", "failed", "scenes_synced", "scenes_failed" }`.
- **POST /v1/upkeep/cleanup** — Query: `dry_run` (default `"true"`), `library` (optional name). Removes orphaned proxy/thumbnail files from storage. Returns `{ "orphan_tenants", "orphan_libraries", "orphan_files", "bytes_freed", "skipped_libraries", "errors", "dry_run" }`.
- **POST /v1/upkeep/recluster** — Force recompute face clusters for all tenants. Stores result in materialized cache. Returns `{ "clusters", "total_faces" }`.
- **POST /v1/upkeep/face-crops** — Backfill face crop thumbnails for faces missing `crop_key`. Query: `batch_size` (default 500). Returns `{ "generated", "skipped", "failed" }`.

## Video chunk API

All under `/v1/video`; require tenant auth. Used by the CLI (`lumiverb ingest`) to process video assets in 30-second chunks. The server owns chunk allocation policy (lease expiration, retry on failure). No video bytes reach the server — only scene rep frame keys and metadata. The CLI generates a unique `worker_id` per session for chunk ownership verification.

- **POST /v1/video/{asset_id}/chunks** — Body: `{ "duration_sec" }`. Initialize chunks for the asset (idempotent). Returns `{ "chunk_count", "already_initialized" }`.
- **GET /v1/video/{asset_id}/chunks/next** — Claim next pending chunk for the asset. Returns 204 if none. On success returns `{ "chunk_id", "worker_id", "chunk_index", "start_ts", "end_ts", "overlap_sec", "anchor_phash", "scene_start_ts", "video_duration_sec", "is_last" }`. Worker must send `worker_id` when completing or failing the chunk.
- **POST /v1/video/chunks/{chunk_id}/complete** — Body: `{ "worker_id", "scenes", "next_anchor_phash", "next_scene_start_ms" }`. `scenes`: list of `{ "scene_index", "start_ms", "end_ms", "rep_frame_ms", "proxy_key", "thumbnail_key", "description", "tags", "sharpness_score", "keep_reason", "phash" }`. Persists scenes, updates next chunk anchor state, marks chunk completed. When all chunks for the asset are complete, marks the asset as `video_indexed`. Returns `{ "chunk_id", "scenes_saved", "all_complete" }`. 409 if chunk not owned by worker.
- **POST /v1/video/chunks/{chunk_id}/fail** — Body: `{ "worker_id", "error_message" }`. Marks chunk failed. Returns `{ "chunk_id", "status": "failed" }`. 409 if chunk not owned by worker.
- **GET /v1/video/{asset_id}/scenes** — Returns all scenes for an asset ordered by `start_ms`. Used by VideoVisionWorker. Response: `{ "scenes": [ { "scene_id", "start_ms", "end_ms", "rep_frame_ms", "thumbnail_key", "description", "tags", "sharpness_score", "keep_reason", "phash" } ] }`.
- **PATCH /v1/video/scenes/{scene_id}** — Body: `{ "model_id", "model_version", "description", "tags" }`. Updates vision results on a scene after describing its rep frame. Response: `{ "scene_id", "status": "updated" }`.
- **POST /v1/video/scenes/{scene_id}/sync** — Body: `{ "asset_id" }`. Enqueues a search sync entry for the given scene. Response: `{ "scene_id", "status": "enqueued" }`.

## Libraries API

All under `/v1/libraries`; require tenant auth (middleware).

- **POST /v1/libraries** — Body: `{ "name", "root_path" }`. Name must be unique per tenant (409 if duplicate). New libraries inherit the tenant's path filter defaults at creation time (subsequent changes to defaults do not affect existing libraries). Returns `{ "library_id", "name", "root_path", "scan_status", "is_public" }` (scan_status initially `"idle"`, is_public initially `false`).
- **GET /v1/libraries** — Query: `include_trashed` (optional, default false). Returns list of libraries with `library_id`, `name`, `root_path`, `scan_status`, `last_scan_at`, `status` (`"active"` or `"trashed"`), `is_public`. Trashed libraries excluded unless `include_trashed=true`.
- **GET /v1/libraries/{library_id}** — Returns single library. 404 if not found.
- **GET /v1/libraries/{library_id}/ignored-paths** — Paths a scan must skip because the user trashed them. Query: `after` (rel_path cursor), `limit` (default 500, max 1000). Returns `{ "items": [{ "rel_path", "reason": "trashed" | "emptied" }], "next_cursor" }`, ordered by `rel_path`. `trashed` = in the trash; `emptied` = the user emptied it from the trash.
- **DELETE /v1/libraries/{library_id}/ignored-paths** — Editor. Body: `{ "rel_paths": [...] }`. Forgets emptied-trash records so the next scan ingests those files again (assets still in the trash are restored with `POST /v1/assets/{id}/restore`). Returns `{ "removed": N }`.
- **PATCH /v1/libraries/{library_id}** — Body: `{ "name", "root_path", "is_public" }` (all optional). Updates library name, root path, and/or public visibility. Use `root_path` to repoint a library after moving source files on disk — existing assets match by `rel_path` so the directory structure must be preserved. Setting `is_public: true` inserts a row in the `public_libraries` control plane table, enabling unauthenticated access. Setting `is_public: false` removes it. Returns full library response including `is_public`.
- **DELETE /v1/libraries/{library_id}** — Soft delete: set library `status` to `"trashed"`, soft-delete all assets (`deleted_at` set). If library was public, removes its `public_libraries` control plane row. Returns 204 on success, 404 if not found, 409 if already trashed.
- **POST /v1/libraries/empty-trash** — Hard delete all trashed libraries for this tenant (cascade: asset_metadata, asset_embeddings, video_scenes, video_index_chunks, assets, library_path_filters, libraries). Removes `public_libraries` control plane rows for any trashed libraries that were public. Returns `{ "deleted": N }`.
- **GET /v1/libraries/health** — Bulk health indicator. Returns `[{ "library_id", "healthy", "pending" }]` for every non-trashed library, in a single SQL call. `pending` is the number of assets that would show up on `repair --dry-run` (missing proxy / vision / embeddings / faces / face embeddings / OCR / video scenes / scene vision / transcription / video probe). `healthy` is `pending == 0`. Used by the libraries page to render a green/orange dot per library without N+1ing `/v1/assets/repair-summary`.
- **GET /v1/libraries/{library_id}/revision** — Lightweight polling endpoint. Returns `{ "library_id", "revision", "asset_count" }`. The `revision` counter increments atomically on asset create/update (ingest) and vision metadata submission. UI clients poll this every 10 seconds and use `revision` in query keys to trigger cache invalidation when data changes.
- **GET /v1/libraries/{library_id}/directory** — Query: `path_prefix` (optional). Returns list of subdirectories within a library root. Used by browse UI for directory navigation.

## Library path filters API

All under `/v1/libraries/{library_id}/filters`; require tenant auth and **admin** API key. Path filters control which files are included or excluded during library ingest (scanner). Patterns use `**`-style globs (case-insensitive); `**` matches across path segments. Validation rejects patterns containing `..` or null bytes.

- **GET /v1/libraries/{library_id}/filters** — Returns `{ "includes": [{ "filter_id", "pattern", "created_at" }], "excludes": [...] }`. 404 if library not found.
- **POST /v1/libraries/{library_id}/filters** — Body: `{ "type": "include"|"exclude", "pattern": "...", "trash_matching": false }`. Creates filter. When `trash_matching` is `true` and type is `exclude`, also trashes all active assets matching the pattern. Returns 201 with `{ "filter_id", "type", "pattern", "created_at", "trashed_count" }`. 400 if pattern invalid, 404 if library not found.
- **POST /v1/libraries/{library_id}/filters/preview** — Body: `{ "type": "exclude", "pattern": "..." }`. Returns `{ "matching_asset_count": N }` — count of active assets matching the pattern. Used by UI to show confirmation before creating an exclude filter that would trash assets.
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

- **POST /v1/assets** — Single-file upsert. Body: `{ "library_id", "rel_path", "file_size", "file_mtime" (ISO8601), "media_type", "force": false }`. Upserts by `(library_id, rel_path)`. Returns `{ "action": "added|updated|skipped" }`.
- **GET /v1/assets/{asset_id}** — Return single asset with full detail (EXIF, video preview info, and `video_facet` for probed videos, else null). 404 if not found or trashed.
- **PUT /v1/assets/{asset_id}/video-facet** — Store a video's probe result, replacing any earlier one. Body (all optional except as noted): `{ "duration_sec", "container", "video_codec", "width", "height"` (display dimensions, rotation applied), `"rotation"` (0/90/180/270 clockwise, default 0), `"frame_rate_num", "frame_rate_den"` (nominal rate as a fraction, e.g. 30000/1001), `"start_timecode"` (`HH:MM:SS:FF`, or `HH:MM:SS;FF` drop-frame), `"drop_frame", "audio_codec", "audio_channels", "audio_sample_rate" }`. The facet's `duration_sec` becomes the asset's `duration_sec`. Validated (timecode `HH:MM:SS[:;]FF`, positive rate and sizes, rotation 0/90/180/270, finite non-negative duration): 422 otherwise. Returns the facet. 400 for non-video assets, 404 if not found or trashed.
- **GET /v1/assets/by-path** — Query: `library_id`, `rel_path`. Look up asset by library and path. Returns asset detail or 404.
- **GET /v1/assets/page** — Query: `library_id` (required), `after` (cursor), `limit` (default 500, max 500), `missing_vision` (optional bool), `missing_embeddings` (optional bool), `missing_faces` (optional bool, filters to assets where `face_count IS NULL`), `missing_video_scenes` (optional bool, filters to video assets where `video_indexed = false` and `duration_sec IS NOT NULL`), `missing_scene_vision` (optional bool, filters to indexed video assets with scenes lacking vision descriptions), `missing_transcription` (optional bool), `missing_probe` (optional bool, video assets without a `video_facets` row), `person_id` (optional, filters to assets with a face matched to this person). Keyset-paginated active assets for bulk reconciliation. Returns `{ "items": [...], "next_cursor" }`.
- **PATCH /v1/assets/{asset_id}** — Update asset fields. Returns updated asset.
- **DELETE /v1/assets/{asset_id}** — Trash a single asset on the user's behalf. Sets `deleted_at` and `deleted_reason = "user"`: the asset stays trashed through rescans while the file is still on disk (ADR-016). Also lays the user's trash over an asset the scanner marked missing, so it stays trashed when the file reappears. Returns 204 on success, 404 if not found or already in the user's trash. Quickwit delete is best-effort (log on failure).
- **DELETE /v1/assets** — Batch soft-delete. Body: `{ "asset_ids": [...], "reason": "user" | "missing" (optional) }`. `"user"` = trashed by a person; survives rescans. `"missing"` = the scanner no longer finds the file; ingest restores it if it reappears. Omitted behaves as `"missing"` (what scanners sent before reasons existed), so a client trashing on a person's behalf **must** send `"user"`. `"user"` also upgrades assets already marked missing. Returns `{ "trashed": [...], "not_found": [...] }`.
- **POST /v1/assets/{asset_id}/artifacts/{artifact_type}** — Multipart file upload. `artifact_type` must be one of: `proxy`, `thumbnail`, `video_preview`, `scene_rep`. Form fields: `file` (binary, required), `width` (int, optional, images only), `height` (int, optional, images only), `rep_frame_ms` (int, required for `scene_rep`, ignored for other types). Streams the upload to disk in 64 KB chunks, computes SHA-256 incrementally, and atomic-renames into place. Updates DB after file is safely on disk. Returns `{ "key", "sha256" }`. Errors: 400 invalid type or missing `rep_frame_ms` for `scene_rep`, 404 asset not found or trashed, 413 file too large.
- **GET /v1/assets/{asset_id}/{proxy|thumbnail}** — Stream proxy or thumbnail file bytes. Returns `application/octet-stream`.
- **GET /v1/assets/facets** — Query: `library_id` (required), `path_prefix` (optional). Returns aggregated filter facets: `{ "media_types", "camera_makes", "camera_models", "lens_models", "iso_range", "aperture_range", "focal_length_range", "has_gps_count", "has_face_count" }`.
- **POST /v1/assets/{asset_id}/faces** — Submit face detections. Body: `{ "detection_model": "insightface"|"apple_vision", "detection_model_version": str, "faces": [{ "bounding_box": dict, "detection_confidence": float, "embedding": [512 floats] | null }] }`. Bounding box: normalized fractions 0.0–1.0, top-left origin. Accepts `{x, y, w, h}` (position+size) or `{x1, y1, x2, y2}` (two corners) — server normalizes to `{x, y, w, h}` before storage. Each detection that re-finds an existing face of the asset (box IoU ≥ 0.3, else embedding cosine distance < 0.45) updates that face in place: it keeps its `face_id`, confirmed person assignment and rejections, while machine-made (unconfirmed) assignments are re-derived. Old faces not re-found are deleted unless a person confirmed them, in which case they are kept as-is. Sets `assets.face_count` (new detections + kept faces). Bumps library revision. Returns 201 `{ "face_count", "face_ids" }`; `face_ids` has one id per submitted detection, in order.
- **POST /v1/assets/{asset_id}/transcript** — Upload or replace an SRT transcript for a video asset. Body: `{ "srt": str, "language": str | null, "source": "manual" | <provider id, e.g. "whisper"> }` (default `"manual"`). Stored as `assets.transcript_source`. Machine output never replaces a manual transcript: a non-manual submission over one returns `{ "status": "kept_manual" }` and changes nothing. Empty `srt` = checked, no speech (`"no_speech"`). Parses SRT to extract plain text for search indexing. Triggers search re-sync. Returns `{ "asset_id", "status": "transcribed" | "no_speech" | "kept_manual" }`. Errors: 400 if not a video or invalid SRT, 404 if asset not found.
- **DELETE /v1/assets/{asset_id}/transcript** — Remove transcript from a video asset. Clears all transcript fields (including `transcript_source`) and re-syncs search. Returns 204.
- **PUT /v1/assets/{asset_id}/note** — Add or update a freeform note. Body: `{ "text": str }`. Stores last author (email) and timestamp. Note text is indexed for search. Returns `{ "asset_id", "note", "note_author", "note_updated_at" }`.
- **DELETE /v1/assets/{asset_id}/note** — Delete a note from an asset. Returns 204.
- **GET /v1/assets/{asset_id}/faces** — List detected faces. Returns `{ "faces": [{ "face_id", "bounding_box", "detection_confidence", "person": { "person_id", "display_name" } | null }] }`. The `person` field is populated when the face is matched to a named person.

## People API

People are tenant-scoped — a person named in one library is recognized across all libraries.

- **GET /v1/people** — Cursor-paginated list of people sorted by face count descending. Query: `after`, `limit` (default 50, max 100), `q` (optional name search, case-insensitive substring match). Returns `{ "items": [{ "person_id", "display_name", "face_count", "representative_face_id", "representative_asset_id", "confirmation_count" }], "next_cursor" }`.
- **POST /v1/people** — Create a named person. Body: `{ "display_name": str, "face_ids": [str] | null }`. If `face_ids` provided, assigns those faces and computes centroid. Returns 409 if any face is already assigned. Returns 201 `PersonItem`.
- **GET /v1/people/{person_id}** — Get a person by ID. Returns `PersonItem` or 404.
- **PATCH /v1/people/{person_id}** — Update display name. Body: `{ "display_name": str }`. Returns `PersonItem` or 404.
- **DELETE /v1/people/{person_id}** — Delete person and all face matches. Returns 204 or 404.
- **GET /v1/people/{person_id}/faces** — Cursor-paginated faces matched to a person. Query: `after`, `limit`. Returns `{ "items": [{ "face_id", "asset_id", "bounding_box", "detection_confidence", "rel_path" }], "next_cursor" }`.
- **GET /v1/faces/{face_id}/crop** — Serve the 128x128 WebP face crop thumbnail. Generated at face submission time from the asset proxy using bounding box + 40% padding. Returns 404 if no crop available (old faces). Immutable caching (1 year).
- **GET /v1/faces/clusters** — Return clusters of unassigned faces. Uses materialized cache; recomputes lazily when dirty (faces added/assigned/unassigned). Query: `limit` (default 20, max 50), `faces_per_cluster` (default 6, max 20). Returns `{ "clusters": [{ "cluster_index", "size", "faces": [...] }], "truncated": bool }`. Not paginated — bounded by `limit` clusters.
- **POST /v1/faces/{face_id}/assign** — Assign a face to a person (stored as confirmed). Body: `{ "person_id": str }` (existing person) or `{ "new_person_name": str }` (creates new person). Clears an earlier rejection of this face for that person. Returns 409 if face is already assigned. Returns `{ "person_id", "display_name" }`.
- **DELETE /v1/faces/{face_id}/assign** — Remove a face from its assigned person. Clears `faces.person_id`, recomputes person centroid, and records a rejection (`face_person_rejections`) so auto-assignment and the upkeep sweep never put the face back on that person. Assigning a cluster to that person skips the face; merging the person carries the rejection to the merge target. Returns 204 or 404.
- **POST /v1/people/{person_id}/merge** — Merge source person into target. Body: `{ "source_person_id": str }`. Reassigns all face matches, updates `faces.person_id`, recomputes centroid, picks best representative face, deletes source. Rejections ("not this person", from un-assigning) carry over to the target. Where a face ends up both matched to and rejected for the merged person, a machine match always loses, and between a confirmation and a rejection the later decision wins, so the result is the same whichever way two people are merged. Serialized via `SELECT ... FOR UPDATE` on source. Returns updated target `PersonItem`. Returns 400 if merging into self.

## Ingest API

Atomic ingest: create + populate assets in one request. The server normalizes the proxy (WebP, 2048px max), generates a thumbnail (WebP, 512px), and stores all provided metadata atomically. If the client sends a WebP proxy already within size limits, the server stores it as-is (no re-encoding).

- **POST /v1/ingest** — Multipart form. Creates asset record AND ingests proxy + metadata atomically. The asset only appears on the server once fully populated. If an asset with the same `(library_id, rel_path)` already exists, it is updated (idempotent). Required fields: `proxy` (file), `library_id`, `rel_path`, `file_size`. Optional: `file_mtime` (ISO8601), `media_type` (default `image/jpeg`), `width`/`height` (source dimensions), `exif` (JSON), `vision` (JSON), `embeddings` (JSON array), `video_facet` (JSON, video only; same shape as `PUT /v1/assets/{id}/video-facet`; 400 if not a video; an invalid facet is logged and ignored so it never blocks ingest). Returns `{ "asset_id", "proxy_key", "proxy_sha256", "thumbnail_key", "thumbnail_sha256", "status", "width", "height", "created" }`. Enforces library path filters: 422 if `rel_path` is excluded. 409 if the user trashed the asset (`deleted_reason = "user"`) or emptied it from the trash (see `ignored-paths`); an asset the scanner marked missing is restored instead.
- **POST /v1/assets/{asset_id}/ingest** — Ingest into an existing asset record. Same proxy + metadata fields minus `library_id`/`rel_path`/`file_size`.

## Trash API

- **DELETE /v1/trash/empty** — Permanently delete trashed assets. Requires admin API key. Body: `{ "asset_ids": ["ast_..."] (optional), "trashed_before": "2026-01-01T00:00:00Z" (optional) }`. Without `asset_ids`, only the user's trash is purged (`deleted_reason = "user"`): assets that are merely missing on disk, such as an unplugged drive, keep their human data until purged by id. Scope: intersection when both provided. Deletes DB rows in FK-safe order, then best-effort proxy/thumbnail file removal and Quickwit delete. Assets the user trashed leave an `ignored_files` record (library, path, SHA-256), because Lumiverb never deletes originals and the file may still be on disk; scans and ingest keep skipping it until it is removed with `DELETE /v1/libraries/{id}/ignored-paths`. Returns `{ "deleted": N }`.

## Projects API

Projects are transient, many-to-many sets of clips for one job (ADR-016), across libraries; what ADR-006 called collections. "Collection" is reserved for a future long-lived container. The same routes are still served at `/v1/collections` and `/v1/public/collections` (deprecated, hidden from the schema) until the macOS/iOS apps move over, and every project response also carries a legacy `collection_id` equal to `project_id`. New ids start `prj_`; ids from before the rename start `col_`.

**Lifecycle**: `status` is `active` or `archived`. Archived projects leave the default list (sidebar, pickers) but keep their clips and stay readable and exportable.

- **POST /v1/projects** — Body: `{ "name", "description" (optional), "sort_order": "manual"|"added_at"|"taken_at" (default "manual"), "visibility": "private"|"shared" (default "private"), "asset_ids": [...] (optional) }`. Creates project owned by current user. When `asset_ids` provided, creates and populates atomically. Returns 201 with `ProjectItem`.
- **GET /v1/projects** — Query: `status` (`active` default, `archived`, `all`). List projects owned by user + shared projects. Returns `{ "items": [ProjectItem] }`. Each item includes `ownership` ("own" or "shared"), resolved `cover_asset_id` and computed `asset_count`.
- **GET /v1/projects/{id}** — Get project detail. Returns `ProjectItem`. 404 if not found or not visible to user.
- **PATCH /v1/projects/{id}** — Body: `{ "name", "description", "visibility", "sort_order", "cover_asset_id", "status" }` (all optional, only provided fields updated). Owner only (403). Returns updated `ProjectItem`. 400 for invalid sort_order/visibility.
- **DELETE /v1/projects/{id}** — Delete project. Owner only (403). Source assets untouched. Returns 204. 404 if not found.
- **POST /v1/projects/{id}/assets** — Body: `{ "asset_ids": [...] }`. Add assets to project. Owner only (403). Idempotent (duplicates ignored via ON CONFLICT DO NOTHING). Rejects trashed assets (404). Returns `{ "added": N }`.
- **DELETE /v1/projects/{id}/assets** — Body: `{ "asset_ids": [...] }`. Remove assets from project. Owner only (403). Does not affect source assets. Returns `{ "removed": N }`.
- **GET /v1/projects/{id}/assets** — Query: `after` (cursor), `limit` (1–1000, default 200). Paginated asset list ordered by project's `sort_order`. Owner or shared visibility required. Returns `{ "items": [ProjectAssetItem], "next_cursor" }`.
- **PATCH /v1/projects/{id}/reorder** — Body: `{ "asset_ids": [...] }`. Reorder assets. Owner only (403). Must include ALL active asset IDs in the project. 400 if partial. Returns `{ "ok": true }`.

**ProjectItem**: `{ "project_id", "name", "description", "cover_asset_id", "owner_user_id", "visibility", "ownership", "sort_order", "asset_count", "created_at", "updated_at" }`

**ProjectAssetItem**: `{ "asset_id", "rel_path", "file_size", "media_type", "width", "height", "taken_at", "status", "duration_sec", "camera_make", "camera_model" }`

**Key behaviors**: Projects are user-owned (`owner_user_id`). Visibility: `private` (owner only), `shared` (all tenant users can view), `public` (anyone with link). Mutations (add/remove/reorder/delete) require ownership. Asset count is computed at query time (no denormalized column). Cover image uses lazy self-healing — if `cover_asset_id` points to a deleted/removed asset, falls back to first-by-position and nulls the stale value. Trashing an asset hides it from projects but preserves the `project_assets` row; restoring the asset restores project membership and position. Hard-deleting (empty trash) removes `project_assets` rows via ON DELETE CASCADE.

**Send to editor (ADR-016 v1):**

- **GET /v1/projects/{id}/export** — Query: `format` (`fcp7` = DaVinci Resolve / Premiere Pro, xmeml v5; `fcpxml` = Final Cut Pro, FCPXML 1.10), `prefix` (optional). Returns the file as an attachment (`Content-Disposition` filename = the project name, unsafe characters replaced). A bin (FCP7) or event (FCPXML) of master clips pointing at the originals: `file://{prefix or the library's root}/{rel_path}`, with duration in frames, nominal frame rate, start timecode (drop-frame aware), display size and audio layout from the video facet; unprobed clips fall back to 30 fps. Pages through every clip (no 1,000 cap). Video only for now: `X-Lumiverb-Skipped-Stills` counts the photos left out. Archived projects export too. 400 unknown format, 404 not found or not visible.
- **GET /v1/export/formats** — `{ "items": [{ "id", "label", "file_extension" }] }` for the format chooser.

**Paging**: `GET /v1/projects/{id}/assets` returns `next_cursor` for smart projects too (text-search smart projects return their relevance-ordered candidate set, up to 5,000, without a cursor). Static projects sorted by `taken_at` page by capture time, `COALESCE(taken_at, file_mtime, created_at)`. A smart project's rating filters read its owner's ratings, so a shared "favorites" project shows every viewer the same clips.

**Public project endpoints (no auth required):**

- **GET /v1/public/projects/{id}** — Returns privacy-stripped project metadata: `{ "project_id", "name", "description", "cover_asset_id", "asset_count" }`. 404 if project not found or not public. Resolved via `public_projects` control plane table.
- **GET /v1/public/projects/{id}/assets** — Query: `after` (cursor), `limit`. Returns privacy-stripped asset list: `{ "items": [{ "asset_id", "media_type", "width", "height", "taken_at", "duration_sec" }], "next_cursor" }`. No rel_path, no camera info, no GPS.
- Asset thumbnails/proxies served via existing `/v1/assets/{id}/proxy?public_project_id={id}` (or the legacy `public_collection_id`) — verifies asset membership in the public project.

## Unified Query API

One endpoint for browsing and searching, across all libraries the caller can see. `/v1/browse` and `/v1/search` were removed in its favor.

- **GET /v1/query** — Filters are repeated `f=prefix:value` params, ANDed together. Prefixes: `query` (text search), `library` (comma-separated ids), `path` (rel_path prefix), `media` (`image`|`video`), `camera_make`, `camera_model`, `lens`, `iso`, `aperture`, `focal_length`, `exposure`, `has_exposure`, `has_gps`, `near`, `date` (`YYYY-MM-DD,YYYY-MM-DD`, either side optional), `favorite` (`yes`), `stars` (`4`, `4+`, `2-4`, `-3`), `color` (comma-separated), `has_color`, `has_rating`, `has_faces` (`yes`|`no`), `person`, `tag`. Unknown prefixes are ignored; `GET /v1/filters/capabilities` lists every filter for generic client rendering. Other params: `sort` (`taken_at` default, `created_at`, `file_size`, `iso`, `exposure_time_us`, `aperture`, `focal_length`, `rel_path`, `asset_id`), `dir`, `after` (cursor), `limit` (1–500, default 200). With a `query:` filter, Quickwit BM25 (or the Postgres ILIKE fallback) produces up to 5,000 candidates, the other filters apply in Postgres, and results are ordered by relevance; each item carries `search_context` `{ "score", "hit_type": "asset"|"scene"|"transcript", "snippet", "start_ms", "end_ms" }`. Rating filters join the caller's own ratings. Returns `{ "items": [QueryItem], "next_cursor", "total_estimate", "search_source": "quickwit"|"postgres_fallback"|null }`.

**QueryItem**: asset fields (`asset_id`, `rel_path`, `file_size`, `media_type`, dimensions, `taken_at`, `duration_sec`, camera and exposure fields, GPS, `face_count`, `thumbnail_key`, `proxy_key`, `created_at`) plus `library_id`, `library_name` and `search_context`.

## Ratings API

User-scoped asset ratings: favorites (heart), stars (1-5), color labels. Each user has independent ratings per asset. Ratings are private — never visible to other users. All endpoints require auth; user identity comes from JWT `sub` or API key `key:{key_id}`.

Rating filters are available on `GET /v1/assets/page` (`?favorite=true`, `?star_min=3`, `?star_max=5`, `?color=red` (comma-separated for multiple), `?has_rating=true`) and on `GET /v1/query` (`f=favorite:yes`, `f=stars:3+`, `f=color:red`, `f=has_rating:yes`). They LEFT JOIN `asset_ratings` for the current user; without rating filters, no JOIN is added (zero cost).

**Face filter**: `?has_faces=true|false` on `GET /v1/assets/page`, `f=has_faces:yes|no` on `GET /v1/query`. `yes` returns only assets with `face_count > 0`; `no` returns assets with no detected faces (face_count=0 or NULL). Applied in SQL.

- **GET /v1/assets/favorites** — List favorited assets across all libraries for the current user, newest first. Query: `after` (cursor), `limit`. Returns: `{ "items": [{ "asset_id", "library_id", "library_name", "rel_path", ... }], "next_cursor" }`. Paginated by `updated_at DESC`.

- **PUT /v1/assets/{asset_id}/rating** — Set or update rating on a single asset. Body: `{ "favorite": bool, "stars": int (0-5), "color": string|null }`. All fields optional — only provided fields are updated. Color values: `red`, `orange`, `yellow`, `green`, `blue`, `purple`, or `null` to clear. If all fields are default (favorite=false, stars=0, color=null), the rating row is deleted. Returns: `{ "asset_id", "favorite", "stars", "color" }`. 404 if asset not found or trashed. 422 for invalid stars/color.
- **PUT /v1/assets/ratings** — Batch rate multiple assets. Body: `{ "asset_ids": [...], "favorite": bool, "stars": int, "color": string|null }`. Same merge semantics as single — only provided fields are updated across all listed assets. Returns: `{ "updated": int }`. 404 if any asset not found. 422 for invalid values or empty asset_ids. Max 1000 asset_ids.
- **POST /v1/assets/ratings/lookup** — Bulk read ratings. Body: `{ "asset_ids": [...] }`. Returns: `{ "ratings": { "asset_id": { "favorite", "stars", "color" }, ... } }`. Assets with no rating are omitted from the map. Max 1000 asset_ids.

Ownership: when a user is deleted via `DELETE /v1/users/{user_id}`, all their ratings are removed from the tenant DB.

## Admin API

Admin routes live under `/v1/admin` and require `Authorization: Bearer {ADMIN_KEY}` (not tenant API keys). If `ADMIN_KEY` is not set, admin routes return 500.

- **POST /v1/admin/tenants** — Body: `{ "name", "plan": "free|pro|enterprise", "email", "vision_api_url", "vision_api_key" }`. Creates tenant, provisions tenant DB (pgvector + Alembic), creates routing row, creates default API key. Returns `{ "tenant_id", "api_key", "database": "provisioned" }`. On failure, cleans up and returns 500.
- **GET /v1/admin/tenants** — Returns list of tenants with `tenant_id`, `name`, `plan`, `status` (no API keys, no vision credentials).
- **PATCH /v1/admin/tenants/{tenant_id}** — Body: `{ "vision_api_url", "vision_api_key", "vision_model_id" }` (all optional; only provided fields are updated). Updates tenant vision API config. Returns `{ "tenant_id", "vision_api_url", "vision_model_id" }`. 404 if tenant not found or deleted.
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

**Query syntax**: unquoted input is loose — `negative space` matches any document containing either word, with a soft ranking boost when both appear as an adjacent phrase. Wrapping input in double quotes requires the exact contiguous phrase — `"negative space"` only matches documents where those two words appear adjacent. Mixed input like `"negative space" beach` is treated as "must contain the phrase AND must contain beach". Quoted phrases are not prefix-expanded (`"disney"` will not match "disneyland"). Unmatched trailing quotes are ignored. Applies to both the Quickwit path and the Postgres fallback.

Text search runs through `GET /v1/query` with an `f=query:...` filter (see Unified Query API). Asset, scene and transcript hits are merged per asset; `search_context.hit_type` says which matched.

**Similarity:**

- **GET /v1/similar** — Find visually similar assets by vector similarity (pgvector). Query params: `asset_id` (required), `library_id` (required), `limit` (default 20, max 100), `offset` (default 0, max 10000). Optional scope filters: `from_ts`, `to_ts` (Unix timestamp seconds, inclusive capture-time range; uses `assets.taken_at`); `asset_types` (comma-separated: `image`, `video`; restricts by `media_type` prefix); `camera_make` and `camera_model` (repeatable; pairs by index, OR across pairs). Returns `{ source_asset_id, hits, total, embedding_available }`. Excludes the source asset from results. If both `from_ts` and `to_ts` are set, `from_ts` must be ≤ `to_ts` (422 otherwise). Person-aware reranking: if the source asset has identified faces, candidates containing the same named person(s) receive a similarity boost (distance *= 0.85).
- **POST /v1/similar/search-by-image** — Body: `{ "library_id", "image_b64", "limit", "offset", "from_ts", "to_ts", "asset_types", "cameras" }`. Upload a query image (base64) and find similar assets. Returns `{ "hits", "total" }`. Server CLIPs the upload — only works for libraries indexed with `model_id="clip"`.
- **POST /v1/similar/search-by-vector** — Body: `{ "library_id", "vector", "model_id", "model_version", "limit", "offset", "from_ts", "to_ts", "asset_types", "cameras", "image_b64" (optional) }`. Search by a pre-computed embedding vector. For clients that embed locally — iOS uses Apple Vision feature prints, macOS uses CLIP via CoreML. The model_id / model_version must match how the library was indexed. **Hybrid mode**: when `image_b64` is also supplied, the server runs InsightFace face detection on the image, embeds each face with ArcFace, runs per-face cosine search against `faces.embedding_vector`, and fuses the identity matches with the scene cosine results via Reciprocal Rank Fusion (k=60). RRF operates on ranks, not raw distances, so it's robust across embedding model spaces. The face path degrades gracefully when no faces are detected, the InsightFace runtime is unavailable, or the image fails to decode — the response falls back to scene-only ranking. Returns `{ "hits", "total" }`.

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
- Do not store or serve source files
