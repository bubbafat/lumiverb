# Lumiverb — Architecture Document
*Version 1.0 | Working document — name TBD*

> **Direction:** [ADR-016: Operating Model](adr/016-operating-model.md) sets where Lumiverb is going. This document describes the system as built. Where the two disagree, ADR-016 wins.

---

## 1. Product Overview

Lumiverb is an AI-powered photo and video library management system for professional photographers, videographers, and serious hobbyists. It provides intelligent search, similarity discovery, and metadata enrichment across large personal media libraries (25,000+ assets).

It is not a photo editor. It is not a cloud backup service. It is the intelligent index and search layer that sits on top of your existing file storage.

### Core Value Proposition

- Natural language search across your entire library ("misty morning shoot in the mountains")
- Visual similarity search ("find more like this")
- AI-generated scene descriptions for every asset, including video
- Full support for RAW files, high-resolution video, and professional formats
- Privacy-first: source files never leave your infrastructure

### Deployment Modes

The same codebase supports two deployment modes:

| Mode | Operator | Cost | Infrastructure |
|---|---|---|---|
| Cloud-hosted | Product team | ~$12/month subscription | GCP (Cloud Run, Cloud SQL, Cloud Storage) |
| Self-hosted | User | Free / one-time fee | Docker Compose on NAS or VPS |

This follows the Immich model: fully open source, no open-core, with a hosted option for convenience.

---

## 2. Architectural Principles

1. **API-first.** Every capability is exposed via the REST API. The CLI, web UI, and Mac agent are all API clients. Nothing bypasses the API.
2. **Originals are read-only.** Anything that reads source files (scanning, probing, proxy generation) runs on a machine that can read them: today the CLI or macOS app on the machine that holds them, and under ADR-016 the brain through a read-only mount. Originals are never written, moved, renamed or uploaded; the API never receives source files.
3. **Stateless API server.** The API server holds no local state. It can be replaced, scaled, or moved without data loss.
4. **Per-tenant isolation.** Each tenant has a dedicated database. Cross-tenant data leakage is architecturally impossible.
5. **Regenerable caches.** Proxies, thumbnails, search indexes and AI outputs are all regenerable from source. Human data in Postgres (person names, corrections, trash, projects, filters, settings) is irreplaceable and is never overwritten automatically by derived data (ADR-016).
6. **Storage abstraction.** All object storage access goes through an abstraction layer supporting Cloud Storage, S3, Backblaze B2, and MinIO (for self-hosted).

---

## 3. System Components

### 3.1 Control Plane

A single shared Postgres database (tiny) with three tables:

```
tenants           — tenant_id, name, plan, status, vision_model_id,
                    transcript_model_id, created_at
ai_machines       — machine_id, tenant_id, name, api_url, api_key, jobs, at_once,
                    enabled, built_in, online, status_error, models, checked_at,
                    created_at
api_keys          — key_id, key_hash, tenant_id, name, label, scopes,
                    role, created_at, last_used_at, revoked_at
users             — user_id, tenant_id, email, password_hash, role,
                    created_at, last_login_at
password_reset_tokens — token_hash, user_id, expires_at, used_at
public_libraries  — library_id, tenant_id, connection_string, created_at
public_projects   — project_id, tenant_id, connection_string, created_at
revoked_tokens    — jti (PK), revoked_at. Server-side JWT revocation.
tenant_db_routing — tenant_id, connection_string, region
```

The control plane handles: tenant provisioning, API key validation, JWT revocation tracking, and routing requests to the correct tenant database. It never stores media metadata.

### 3.2 Tenant Database (per tenant)

Each tenant gets a dedicated Postgres database on the same Postgres instance (scales to hundreds of tenants before needing separate instances).

**Core tables:**

```
libraries         — library_id, name, root_path, status, scan_status,
                    last_scan_at, is_public, revision (int), created_at,
                    updated_at
assets            — asset_id, library_id, rel_path, sha256, file_size,
                    file_mtime, media_type, width, height, duration_sec,
                    proxy_key, proxy_sha256, thumbnail_key, thumbnail_sha256,
                    video_preview_key, video_indexed (bool),
                    exif (JSON), exif_extracted_at, camera_make,
                    camera_model, taken_at, gps_lat, gps_lon, iso,
                    exposure_time_us, aperture, focal_length,
                    focal_length_35mm, lens_model, flash_fired, orientation,
                    availability, status, error_message,
                    created_at, updated_at, deleted_at, search_synced_at
video_scenes      — scene_id, asset_id, scene_index, start_ms, end_ms,
                    rep_frame_ms, proxy_key, thumbnail_key,
                    rep_frame_sha256, description, tags (JSONB),
                    sharpness_score, keep_reason, phash, created_at,
                    search_synced_at
video_index_chunks — chunk_id, asset_id, chunk_index, start_ms, end_ms,
                    status, worker_id, claimed_at, lease_expires_at,
                    completed_at, error_message, anchor_phash,
                    scene_start_ms, created_at
asset_metadata    — metadata_id, asset_id, model_id, model_version,
                    generated_at, data (JSONB)
asset_embeddings  — embedding_id, asset_id, model_id, model_version,
                    embedding_vector vector(512), created_at
system_metadata   — key, value, updated_at
```

**Views:**
- `active_assets` — non-trashed assets (deleted_at IS NULL). All pipeline queries use this view. Trashing a library sets deleted_at on all its assets, removing them from this view immediately.

**Key constraints:**
- `video_index_chunks`: a failed chunk stays failed for the rest of its run (the client ends the run at it, and the clip's scenes job is charged a failure); the next run's `POST /v1/video/{asset_id}/chunks` puts it back to `pending`, under the scheduler's back-off.
- `video_scenes`: unique on `(asset_id, scene_index)`; the server numbers a clip's scenes as each chunk completes, after those stored.
- `assets`: unique constraint on `(library_id, rel_path)` — ingest upserts by this key.

**Artifact lifecycle:**
- When a library is trashed (`DELETE /v1/libraries/{id}`), all its assets get `deleted_at` set (soft-deleted). Trashed assets are excluded from search sync sweeps.
- If a proxy or thumbnail key points to a missing file, the endpoint returns 404 and clears the stale key from the asset record.
- On empty-trash (hard delete), artifact files in object storage are deleted best-effort, then DB rows are removed in FK-safe order.

**Face detection tables (ADR-009):**

```
faces             — face_id, asset_id, bounding_box_json,
                    embedding_vector vector(512), detection_confidence,
                    detection_model, detection_model_version, created_at
people            — person_id, display_name, created_by_user,
                    centroid_vector vector(512), confirmation_count,
                    representative_face_id, created_at
face_person_matches — face_id, person_id, confidence,
                    confirmed (bool), confirmed_at
```

Face detection runs client-side in the CLI via InsightFace (buffalo_l). Detected faces with ArcFace 512-dim embeddings are POSTed to `POST /v1/assets/{id}/faces`. The `assets.face_count` column tracks detection status (NULL = unprocessed, 0 = no faces, N = N faces). Face crops (128x128 WebP) are generated server-side from the asset proxy on submission.

Person clustering uses cosine distance on ArcFace embeddings (threshold 0.55) with a materialized cache in `system_metadata` (dirty flag invalidation). The People page shows clusters for naming. When a user names a cluster, all faces in it are assigned to a person. New faces are auto-assigned to known people on ingest if centroid distance < 0.45. The upkeep sweep periodically propagates assignments for untagged faces.

**pgvector:**

The `pgvector` extension is enabled on the Postgres instance at provisioning time. Enables `vector` column type and approximate nearest-neighbor index (`hnsw`) for similarity queries. Used for asset embeddings (CLIP ViT-B-32), face embeddings (ArcFace via InsightFace), and face-to-face similarity for person clustering.

### 3.3 API Server

Python + FastAPI + SQLModel. Stateless. Runs in Docker (self-hosted) or Cloud Run (cloud-hosted).

Responsibilities:
- Authenticate requests via API key or JWT → route to tenant DB
- Security headers middleware (CSP, X-Frame-Options, X-Content-Type-Options)
- Rate limiting on auth endpoints (in-memory sliding window per IP)
- JWT token lifecycle: 1-hour access tokens with 7-day refresh window, server-side revocation via `revoked_tokens` table
- Library and asset CRUD
- User management (email/password auth, JWT sessions)
- Atomic ingest (proxy + metadata in one request)
- File serving (thumbnails, proxies, video previews — never source files)
- Search endpoint (BM25 via Quickwit)
- Similarity search endpoint (pgvector nearest-neighbor on CLIP embeddings, person-aware reranking)
- People and face management (clustering, assignment, merge)
- Video chunk coordination (scene segmentation metadata)
- Search sync (timestamp-based sweep to keep Quickwit in sync)
- Upkeep (search sync, face propagation, orphaned file cleanup, revoked token cleanup)

### 3.4 The CLI and the Mac app

API clients. The CLI (`src/client/cli/`) scans libraries (`lumiverb scan`: proxies, thumbnails, EXIF, previews, uploaded through `POST /v1/ingest`), browses, searches and administers. It doesn't process: `lumiverb enrich` asks the scheduler to do a producer's work now (`POST /v1/producers/run`). The macOS app scans and reports file changes; its built-in AI retires (ADR-016 phase 2). Neither has direct Postgres, Quickwit or object storage access.

The processing both the scheduler and the CLI's scan use lives in `src/processing/`: the API client (`api.py`), this machine's settings (`machine.py`: library root map, caches, how video is rendered), scanning (`scan.py`, `ingest.py`), the account's AI machines (`ai_pool.py`, `job_guard.py` and its guards), failure reports, the producers' settings as the server says them, and the media tools (`video/`, `proxy/`, `workers/`). Nothing there imports the CLI or the server, and nothing in `src/server` imports `src/client`.

### 3.5 Processing: producers and the scheduler

All processing runs on the brain, in the scheduler (`src/server/scheduler/`, systemd unit `lumiverb-scheduler`, beside the API, sharing its database). It ranks every job of every account (tier, then oldest first), fills each pool's free slots, and saves results through the API's own routes with a per-account key it makes at start. What's due comes from the reconciler (`repository/lineage.py`): missing, made from a file that changed, or (tier 4) made with another producer version or settings than now.

**Producers.** Each kind of derived artifact has one producer, one folder each: `src/producers/<artifact>/`. Its `__init__.py` declares a `ProducerSpec` (`src/producers/contract.py`); its `work.py` holds its `Work`. Everything that lists producers reads the registry (`src/producers/__init__.py`): lineage and the reconciler, the scheduler's kinds, queue, pools and runners, the AI jobs and where each account keeps their models, the asset page's `missing_*` filters and the repair summary, what goes with an artifact when it's made again, pauses, and `GET /v1/producers` (Settings → Processing renders it as it comes).

**The runner.** A producer supplies `make(clip)` (the artifact, or raise `Failed` / `Waits` / `NotTried`) and `save(client, made)`. Everything else is the runner's (`src/producers/runner.py`), the same for every producer: stopping (once the scheduler stops nothing more is made or saved, through a client that refuses), saving (right after making, or a job's clips together for a producer that sets `together`, or each part as a generator yields it), no NUL in what's sent, and whose an error is. The clip's (the API refused what was made: 400, 413, 422) is charged now and backs off (5 minutes, doubling up to a day, given up after 10 tries); the API or its database away (401, 403, 408, 429, 502-504, a transport error) charges nothing; anything else is a crash, uncharged but counted, and charged after `CRASHES_BEFORE_CHARGE` in a row; two clips crashing in a row leave the rest of the job waiting. An AI producer hands its failures to its job's guard, which charges a clip only when a machine checks out fine.

**Pools and AI jobs.** A spec names its `Pool` (`src/producers/pools.py` for today's shared ones): so many slots, or this machine's setting that sizes it (`sized_by`), or an AI job's machines (`job`: each account's own, sized by its online machines times the job's `per_request`), and whether its jobs hold back AI machines sharing the GPU (`gpu_hold`). An `AiJob` declares its label, its guard (how its machines are checked and called), its default model and whether the scheduler's own machine can do it; the account's model for it is kept under its name (`tenants.ai_job_models`).

**Models.** The GPU models are the scheduler's, not an account's (`scheduler/models.py`): CLIP loaded once per model and weights, and one face-detection process (ONNX Runtime leaks, so it's replaced after so many photos), shared by every account and let go of when unused. A photo it hasn't answered for in 300 s (`DETECT_TIMEOUT_SEC`, `src/producers/faces/detect.py`) is given up as a crash (counted, uncharged) and the process replaced.

**Pausing.** One switch per processing action (Scans, Upkeep, each producer), paused and resumed on its own, with a scope: `work` (nothing of it starts) or a producer's `redo` alone (its stale clips wait). Both are rows of `producer_pauses`, read by the scheduler every second on one path; a new producer version stops its redo until an admin resumes it.

**Settings.** The scheduler reads this machine's way of processing from its environment (`src/server/scheduler/settings.py`: `LUMIVERB_ROOT_MAP`, `LUMIVERB_ANALYSIS_PROXY_*`, `LUMIVERB_GPU_DECODES`, `LUMIVERB_RENDER_CONCURRENCY`, `LUMIVERB_ANALYSIS_CACHE_GB`, `LUMIVERB_FACE_BATCHES_PER_PROCESS`, `LUMIVERB_API_URL`; `/etc/lumiverb/env` on the brain), never the CLI's config.

**Video scene indexing** is saved as it's found: the scenes producer initializes a video's 30-second chunks (`POST /v1/video/{asset_id}/chunks`), claims each (`.../chunks/next`), segments it and completes it with its scenes, all through the runner's client; the server marks the video `video_indexed` once every chunk is done.

#### Adding a producer

A producer ships as one folder (ADR-016's test, `tests/test_producer_contract.py::test_a_producer_is_one_folder`):

1. `src/producers/<artifact>/__init__.py` sets `PRODUCER = ProducerSpec(...)`: `artifact`, `producer` (recorded in lineage), `version` (bump when its output changes), `media`, `title`, `applies` and `made` (SQL on `active_assets a`), its output-affecting `settings`, `needs` (what it's made from), `redo_also` (what goes with it when it's made again), and to be scheduled `kind`, `flag` (`missing_<something>`), `run` (`"src.producers.<artifact>.work:<Class>"`), `tier`, `pool`, `batch`, `storage` (it reads the originals) and `unit` (`clip` or `second`). It imports only `contract.py`, `prompts.py` and `pools.py`.
2. `src/producers/<artifact>/work.py` defines the `Work`: `make(clip)` returns what `save` sends (use `self.original(clip)` for the file on storage, `self.acct.proxy_cache(...)` / `self.acct.analysis_cache` for proxies, `self.lineage(clip, used=...)` for what a save records), and `save(client, made)` posts it through the given client.
3. A server route that takes the artifact, with `require_lineage(body.lineage, "<artifact>")`, records it with `lineage.record(...)`.

Whose an error is, as the runner judges it:

- An API error raised inside `make()` (a proxy fetched from the server, say) is judged by `whose()`, like a save's: refused (400, 413, 422) is the clip's; the API away is nobody's; anything else is a crash.
- `OSError` from `make()` is this machine's (no ffmpeg or ffprobe, a disk or mount error): a crash, counted, never the clip's. That holds for probe, CLIP, transcripts and OCR; only output that can't be read (ffprobe refusing the file, a proxy PIL can't open) is the clip's.
- Scenes with no analysis proxy in this machine's cache wait for it (so do their descriptions); a transcript with none is charged.
- A save that fails with anything but an API error (the cache's disk full) is a crash, counted.
- A scene whose frame can't be read, or that the model describes as nothing, is its video's failure, charged once the video's other scenes are saved.

Nothing else is edited: the kinds and their redo kind, the queue, the pool (declare a new `Pool` in the folder, or name a shared one), an AI job (declare an `AiJob` with its guard in the folder), the account's model storage, the page filter and summary count, Settings → Processing, pauses and failures all follow. A producer that reads the originals (EXIF, say) sets `storage=True` and calls `self.original(clip)`: jobs then go only to libraries whose storage was reachable at the last look, and the file missing or the storage gone is handled for it.

**Search sync:**
Search sync is timestamp-based: assets and video scenes have a `search_synced_at` column. The `POST /v1/upkeep/search-sync` endpoint sweeps records where `search_synced_at` is stale, builds Quickwit documents, and ingests them. The CLI `lumiverb maintenance search-sync --all` command triggers this for the account. Inline sync also runs on each ingest. Quickwit is a regenerable cache — if lost, run search-sync to rebuild.

### 3.6 Search Engine (Quickwit)

Quickwit provides BM25 full-text search over AI descriptions and metadata. Runs in Docker.

Sync is timestamp-based: assets and video scenes track `search_synced_at`. The upkeep endpoint sweeps stale records and ingests them into Quickwit. Quickwit is a regenerable cache — if lost, run search-sync to rebuild.

### 3.7 Object Storage

All proxies, thumbnails, and (optionally) exported assets stored in object storage.

Abstraction layer supports:
- GCP Cloud Storage (cloud-hosted)
- AWS S3 / S3-compatible (self-hosted or cloud)
- Backblaze B2 (self-hosted, cost-optimised)
- MinIO (fully local self-hosted)

Key naming convention: `{tenant_id}/{asset_id}/proxy.jpg`, `{tenant_id}/{asset_id}/thumb.jpg`

---

## 4. Data Flow

### 4.1 Ingest Flow

```
Local filesystem
    → CLI scans directory, discovers media files
    → For each image:
        → Generate WebP proxy (2048px max) + thumbnail (512px) in memory
        → Extract EXIF metadata (camera, GPS, taken_at, duration)
        → Run vision AI (OpenAI-compatible) → description + tags
        → POST /v1/ingest (multipart: proxy + all metadata, atomic)
        → API normalizes proxy, generates thumbnail, stores everything
        → API attempts inline search sync to Quickwit
        → Asset appears fully populated on first creation
    → For each video (stage 1):
        → Extract poster frame, generate proxy/thumbnail
        → Extract EXIF metadata
        → Generate 10-second preview MP4
        → POST /v1/ingest (atomic)
    → For each video (stage 2 — scene indexing):
        → POST /v1/video/{asset_id}/chunks (init 30-sec chunks)
        → Loop: claim chunk → segment scenes → extract rep frames → complete
        → When all chunks complete, server marks video_indexed=true
```

### 4.2 Search Sync Flow

```
Inline (on each ingest):
    → API calls try_sync_asset() after storing metadata
    → Builds Quickwit document, ingests, sets search_synced_at

Periodic sweep (CLI or upkeep endpoint):
    → POST /v1/upkeep/search-sync
    → Server finds assets/scenes where search_synced_at is stale
    → Builds Quickwit documents from Postgres metadata
    → Ingests to Quickwit in batches
    → Updates search_synced_at on each record
```

### 4.3 Search Flow

```
Client calls GET /search?q=...&library_id=...
    → API queries Quickwit BM25
    → API enriches results with asset metadata from Postgres
    → Returns paginated asset list with thumbnails
```

### 4.4 Similarity Flow

```
Client calls GET /v1/similar?asset_id=...&library_id=...
    → API fetches asset's CLIP embedding from asset_embeddings
    → Runs pgvector nearest-neighbor search (excluding self)
    → Applies optional scope filters (date range, media type, camera)
    → Returns ranked similar assets with similarity scores
```

---

## 5. Technology Stack

| Layer | Technology | Rationale |
|---|---|---|
| API server | Python 3.12, FastAPI, SQLModel | Familiar, Cursor-optimised, proven in PoC |
| Database | PostgreSQL 18 + pgvector | Per-tenant isolation, JSONB for metadata, vector similarity (phase 2) |
| Migrations | Alembic | Standard, works with SQLModel |
| Search | Quickwit | Columnar BM25, Docker-friendly, proven in PoC |
| Object storage | Abstracted (GCS / S3 / B2 / MinIO) | Deployment-mode flexibility |
| AI inference | OpenAI-compatible (configurable) | Supports any vision model (e.g. qwen3-visioncaption-2b via LM Studio) |
| CLIP embeddings | open-clip-torch (ViT-B/32) | 512-dim vectors for similarity search |
| CLI | Python, Typer | Shares models with API server |
| Web UI | React, TypeScript, Tailwind | Required for media grid, virtualized scroll |
| Mac agent | Swift / Electron TBD | Filesystem access, background service |
| Cloud platform | GCP | Employee discounts, known infrastructure |
| Container | Docker Compose (self-hosted), Cloud Run (cloud) | Same image, different orchestration |
| Auth | Hybrid: email/password + JWT (web), API keys (CLI/automation). 1h access tokens, 7d refresh window, server-side revocation. Rate-limited auth endpoints. | Self-hosted, no external auth service dependency |

---

## 6. Multi-Tenancy Model

### 6.1 Tenant Isolation

Each tenant has a dedicated Postgres database. The control plane routes each API request to the correct database using JWT claims or API key → tenant_id → connection_string lookup.

Benefits:
- GDPR export = `pg_dump tenant_db`
- GDPR delete = `DROP DATABASE tenant_db`
- Zero cross-tenant data leakage (architectural guarantee)
- Per-tenant backup, restore, and migration
- Schema migrations can be applied tenant-by-tenant

### 6.2 Tenant Provisioning

Admin-provisioned initially. Self-service signup added later (same underlying provisioning logic, adds registration endpoint + UI).

Provisioning steps:
1. Create record in control plane `tenants` table
2. Create tenant database
3. Run Alembic migrations against tenant database
4. Create initial API key
5. Return connection details

### 6.3 Multi-Region (Future)

Pattern: per-tenant region affinity. Tenant picks region at signup. Control plane routes to home region. No cross-region replication needed for tenant data — only the control plane DB needs multi-region replication (tiny).

Staged rollout:
- Now: single region (GCP us-central1)
- ~100 tenants: add second region as passive replica
- ~300 tenants: per-tenant region selection at signup
- ~500 tenants: multi-region HA as premium tier

---

## 7. Storage Economics

### 7.1 Per-Asset Storage

| Asset type | Size | Notes |
|---|---|---|
| Thumbnail (JPEG 400px) | ~20 KB | Served in grid views |
| Proxy (JPEG 2048px) | ~200 KB | Served for AI inference and detail views |
| Postgres metadata | ~8 KB | Per asset including AI description |
| Quickwit index | ~1.5 KB | Per asset |
| **Total** | **~230 KB** | Proxies dominate at 87% |

### 7.2 At Scale

| Corpus size | Storage | Monthly cost (GCS) |
|---|---|---|
| 100K images | ~23 GB | ~$0.53 |
| 1M images | ~230 GB | ~$5.30 |
| 834 customers × 25K avg | ~4.8 TB | ~$110 |

### 7.3 Infrastructure at 834 Customers (~$10K MRR)

| Component | Monthly cost |
|---|---|
| Object storage (4.8 TB) | ~$110 |
| Cloud SQL (Postgres) | ~$100 |
| Cloud Run (API server) | ~$50 |
| Quickwit VM | ~$50 |
| Vision AI API (tenant-paid) | ~$100–200 |
| **Total** | **~$500–600** |
| **Net revenue** | **~$9,400–9,500** |

---

## 8. Redundancy and Backup

### 8.1 What Must Be Backed Up

Only Postgres is irreplaceable. Everything else is regenerable:
- Proxies / thumbnails → regenerable from source files via `lumiverb ingest`
- Quickwit index → regenerable via search-sync
- AI descriptions → regenerable by re-running ingest

### 8.2 Postgres Backup Strategy

- Managed Cloud SQL with automated daily backups
- Point-in-time recovery (PITR) enabled
- Multi-AZ standby (~60s RTO on instance failure)
- Nightly `pg_dump` to object storage as second layer
- S3 versioning and Object Lock enabled day one

### 8.3 Object Storage

GCS standard tier provides 11 nines durability with built-in multi-AZ replication. No additional backup needed.

---

## 9. API Design Principles

*(Full API specification in separate document: `api-design.md`)*

- RESTful resource-oriented design
- All endpoints require authentication (`Authorization: Bearer {jwt_or_api_key}`)
- Tenant context derived from JWT claims or API key — never passed as parameter
- Pagination via cursor (not offset) for large collections
- Consistent error envelope: `{error: {code, message, details}}`
- OpenAPI spec generated from FastAPI route definitions
- Versioning via URL prefix: `/v1/...`
- File uploads via multipart form (proxies, thumbnails)
- File serving via signed URLs (object storage) or direct proxy (self-hosted)

---

## 10. Build Sequence

### Phase 1: Foundation (CLI + API)
- Control plane + tenant provisioning
- Tenant database schema + Alembic migrations
- Core API endpoints (libraries, assets, ingest, search)
- Local agent CLI (ingest, repair, search, similar)
- Client-side vision AI integration
- Search sync + Quickwit integration
- Docker Compose for self-hosted deployment

### Phase 2: Search + Discovery
- Full-text search endpoint
- Similarity search endpoint
- Video scene indexing (CLI-driven, server-coordinated chunk allocation)
- Enhanced ingest: sharpness scoring, face detection
- CLI search commands

### Phase 3: Web UI
- React + TypeScript + Tailwind
- Virtualized media grid
- Search interface
- Asset detail view (with AI description, metadata)
- Library management

### Phase 4: Mac Agent
- Native Mac app or Electron
- Background filesystem watching
- Menu bar status
- Library configuration UI
- Auto-ingest on file changes

### Phase 5: User Accounts (Done)
- Email + password auth with JWT sessions (see ADR: user-accounts.mdc)
- CLI bootstrap: `lumiverb create-user`
- Password reset via SMTP
- Operators add users via CLI; no public signup flow in v1
- Settings page with API key management (admin/editor)

---

## 11. Self-Hosted Deployment

`docker-compose.yml` includes:
- `api` — FastAPI server
- `postgres` — PostgreSQL 18
- `quickwit` — Search engine
- `minio` — Object storage (optional, can point at S3/B2)

Configuration via environment variables. No cloud dependencies required. A NAS or $5/month VPS is sufficient for personal use.

---

## 12. What Was Proven in the PoC

The following algorithms and patterns are extracted from the PoC codebase (`media-search`) and reimplemented in the new architecture:

- Video scene segmentation and representative frame extraction
- Vision AI integration and analysis pipeline
- BM25 similarity search with adaptive threshold
- Quickwit schema and index management
- EXIF extraction, sharpness scoring, face detection
- Proxy format: WebP (compact, widely supported)

The PoC codebase is frozen as a reference. The new codebase is a clean start — no migration path, no backward compatibility burden.
