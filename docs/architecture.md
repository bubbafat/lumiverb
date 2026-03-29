# Lumiverb — Architecture Document
*Version 1.0 | Working document — name TBD*

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
2. **Local agents stay local.** Anything that touches source files (scanning, proxy generation) runs on the machine where files live. The API never receives source files.
3. **Stateless API server.** The API server holds no local state. It can be replaced, scaled, or moved without data loss.
4. **Per-tenant isolation.** Each tenant has a dedicated database. Cross-tenant data leakage is architecturally impossible.
5. **Regenerable caches.** Proxies, thumbnails, and search indexes are all regenerable from source. Only Postgres is irreplaceable.
6. **Storage abstraction.** All object storage access goes through an abstraction layer supporting Cloud Storage, S3, Backblaze B2, and MinIO (for self-hosted).

---

## 3. System Components

### 3.1 Control Plane

A single shared Postgres database (tiny) with three tables:

```
tenants           — tenant_id, name, plan, status, vision_api_url,
                    vision_api_key, vision_model_id, created_at
api_keys          — key_id, key_hash, tenant_id, name, label, scopes,
                    role, created_at, last_used_at, revoked_at
users             — user_id, tenant_id, email, password_hash, role,
                    created_at, last_login_at
password_reset_tokens — token_hash, user_id, expires_at, used_at
public_libraries  — library_id, tenant_id, connection_string, created_at
tenant_db_routing — tenant_id, connection_string, region
```

The control plane handles: tenant provisioning, API key validation, and routing requests to the correct tenant database. It never stores media metadata.

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
- `video_index_chunks`: failed chunks are automatically reset to `pending` on the next `claim_next_chunk` call for the same asset, preventing permanent pipeline stalls after transient errors.
- `assets`: unique constraint on `(library_id, rel_path)` — ingest upserts by this key.

**Artifact lifecycle:**
- When a library is trashed (`DELETE /v1/libraries/{id}`), all its assets get `deleted_at` set (soft-deleted). Trashed assets are excluded from search sync sweeps.
- If a proxy or thumbnail key points to a missing file, the endpoint returns 404 and clears the stale key from the asset record.
- On empty-trash (hard delete), artifact files in object storage are deleted best-effort, then DB rows are removed in FK-safe order.

**Phase 2 tables (schema created now, populated later):**

```
faces             — face_id, asset_id, bounding_box_json,
                    embedding_vector vector(512), detection_confidence,
                    created_at
people            — person_id, display_name, created_by_user, created_at
face_person_matches — face_id, person_id, confidence,
                    confirmed (bool), confirmed_at
```

These tables exist from day one so phase 2 requires no schema migration surprises. All nullable/empty until face workers run.

**pgvector:**

The `pgvector` extension is enabled on the Postgres instance at provisioning time. Enables `vector` column type and approximate nearest-neighbor index (`ivfflat` or `hnsw`) for similarity queries. Used for face embeddings (phase 2) and optionally image-level semantic embeddings (future).

A dedicated vector database (Qdrant etc.) is not used in v1. Revisit at phase 2 when real embedding data exists to benchmark against pgvector at tenant scale.

### 3.3 API Server

Python + FastAPI + SQLModel. Stateless. Runs in Docker (self-hosted) or Cloud Run (cloud-hosted).

Responsibilities:
- Authenticate requests via API key or JWT → route to tenant DB
- Library and asset CRUD
- User management (email/password auth, JWT sessions)
- Atomic ingest (proxy + metadata in one request)
- File serving (thumbnails, proxies, video previews — never source files)
- Search endpoint (BM25 via Quickwit)
- Similarity search endpoint (pgvector nearest-neighbor on CLIP embeddings)
- Video chunk coordination (scene segmentation metadata)
- Search sync (timestamp-based sweep to keep Quickwit in sync)
- Upkeep (periodic cleanup of orphaned files)

### 3.4 Local Agent (CLI first, then Mac app)

Runs on the machine where source files live. Communicates only via the API.

Responsibilities:
- Filesystem scanning (recursive, respects path filters)
- Deduplication (checks `rel_path` against API before upload)
- Proxy generation (WebP 2048px max; TIFFs use Pillow fallback for large files)
- EXIF and metadata extraction
- Vision AI captioning (OpenAI-compatible endpoint, configurable)
- CLIP embedding generation (open-clip-torch, ViT-B/32)
- Uploads proxy + all metadata to API atomically via `POST /v1/ingest`
- Video poster frame extraction, 10-second preview generation

The local agent has no direct Postgres, Quickwit, or object storage access. The API is its only interface.

### 3.5 Processing Model

There are no server-side worker queues. All processing happens client-side via the CLI `ingest` command, which is the single entry point for assets.

**Image ingest (client-side):**
The CLI scans the filesystem, then for each image:
1. Generates JPEG proxy, then converts to WebP (2048px max)
2. Extracts EXIF metadata (camera, GPS, duration, taken_at, lens, ISO, aperture, etc.)
3. Runs vision AI (OpenAI-compatible API) to generate description and tags
4. Generates CLIP embedding (ViT-B/32, 512-dim vector)
5. Calls `POST /v1/ingest` with proxy + all metadata atomically
6. Server normalizes proxy, generates thumbnail (512px), stores everything
7. The asset only appears on the server once fully populated

**Video ingest (client-side, stage 1):**
1. Gets source dimensions via ffprobe
2. Extracts poster frame and generates proxy/thumbnail
3. Extracts EXIF metadata
4. Generates 10-second preview MP4 (capped at 720p)
5. Calls `POST /v1/ingest` atomically

**Video scene indexing (server-coordinated):**
After video ingest, the CLI uses the video chunk API to process scenes:
1. `POST /v1/video/{asset_id}/chunks` — initialize 30-second chunks
2. `GET /v1/video/{asset_id}/chunks/next` — claim next chunk
3. Client segments scenes, extracts rep frames, uploads via artifact API
4. `POST /v1/video/chunks/{chunk_id}/complete` — submit scene metadata
5. When all chunks complete, server marks asset as `video_indexed`

**Search sync:**
Search sync is timestamp-based: assets and video scenes have a `search_synced_at` column. The `POST /v1/upkeep/search-sync` endpoint sweeps records where `search_synced_at` is stale, builds Quickwit documents, and ingests them. The CLI `lumiverb maintenance search-sync` command triggers this. Inline sync also runs on each ingest. Quickwit is a regenerable cache — if lost, run search-sync to rebuild.

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
| Database | PostgreSQL 16 + pgvector | Per-tenant isolation, JSONB for metadata, vector similarity (phase 2) |
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
| Auth | Hybrid: email/password + JWT (web), API keys (CLI/automation) | Self-hosted, no external auth service dependency |

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
| AI workers | ~$100–200 |
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
- Video worker (scene segmentation, rep frames)
- Metadata worker (sharpness, face detection)
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
- `postgres` — PostgreSQL 16
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
