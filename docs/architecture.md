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
tenants           — tenant_id, name, plan, status, created_at
api_keys          — key_hash, tenant_id, name, scopes, created_at
tenant_db_routing — tenant_id, connection_string, region
```

The control plane handles: tenant provisioning, API key validation, and routing requests to the correct tenant database. It never stores media metadata.

### 3.2 Tenant Database (per tenant)

Each tenant gets a dedicated Postgres database on the same Postgres instance (scales to hundreds of tenants before needing separate instances).

**Core tables:**

```
libraries         — library_id, name, root_path, scan_status, created_at
assets            — asset_id, library_id, sha256, file_path, file_size,
                    media_type, width, height, duration_ms, captured_at,
                    proxy_key, proxy_sha256, thumbnail_key, thumbnail_sha256,
                    availability, created_at
video_scenes      — scene_id, asset_id, start_ms, end_ms, rep_frame_ms,
                    proxy_key, thumbnail_key, rep_frame_sha256
asset_metadata    — asset_id, exif_json, sharpness_score, face_count,
                    ai_description, ai_description_at,
                    embedding_vector vector(512)  -- nullable, populated phase 2
search_sync_queue — asset_id, scene_id, operation, status, created_at
worker_jobs       — job_id, job_type, asset_id, status, worker_id,
                    claimed_at, completed_at, error
system_metadata   — key, value (schema version, last sync, etc.)
```

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
- Authenticate requests via API key → route to tenant DB
- Library and asset CRUD
- Work queue management (claim jobs, post results)
- File serving (thumbnails, proxies — never source files)
- Search endpoint (BM25 via Quickwit)
- Similarity search endpoint
- Webhook delivery (future)

### 3.4 Local Agent (CLI first, then Mac app)

Runs on the machine where source files live. Communicates only via the API.

Responsibilities:
- Filesystem scanning (recursive, respects ignore patterns)
- SHA256 deduplication (checked against API before upload)
- Proxy and thumbnail generation (in memory, never writes source files externally; TIFFs use Pillow to avoid libvips/libtiff memory cap on large files)
- EXIF and metadata extraction
- Uploads proxy + thumbnail + metadata to API
- Maintains local SQLite: `filepath → asset_id` mapping

The local agent has no direct Postgres, Quickwit, or object storage access. The API is its only interface.

### 3.5 AI Workers

Stateless worker processes. Can run anywhere with API access. Claim jobs from the work queue via lease-based claiming (prevents duplicate processing).

**Worker types:**
- `vision_worker` — runs Moondream against proxy images, generates AI descriptions
- `video_worker` — scene segmentation, rep frame extraction, per-scene vision analysis
- `embedding_worker` — generates embedding vectors for similarity search (future)
- `metadata_worker` — sharpness scoring, face detection
- `face_worker` — face detection, embedding generation, cluster assignment (phase 2)

Workers poll the API for available jobs. They never access object storage directly for source files — they fetch proxies via the API.

### 3.6 Search Engine (Quickwit)

Quickwit provides BM25 full-text search over AI descriptions and metadata. Runs in Docker.

The `search_sync_queue` table feeds a sync process that keeps Quickwit in sync with Postgres. Quickwit is a regenerable cache — if lost, run search-sync to rebuild.

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
    → Local Agent scans, hashes files
    → Agent generates proxy (JPEG, max 2048px) + thumbnail (JPEG, 400px) in memory
    → Agent extracts EXIF metadata
    → Agent calls POST /assets with proxy, thumbnail, metadata, SHA256
    → API checks SHA256 — if exists, returns existing asset_id (dedup)
    → API stores metadata in tenant DB
    → API stores proxy + thumbnail in object storage
    → API enqueues vision_worker job
    → API returns asset_id to agent
    → Agent writes filepath → asset_id to local SQLite
```

### 4.2 AI Processing Flow

```
vision_worker polls GET /jobs/claim?type=vision
    → Fetches proxy via GET /assets/{id}/proxy
    → Runs Moondream inference
    → Posts result via POST /jobs/{id}/complete with ai_description
    → API updates asset_metadata, enqueues search_sync
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
Client calls GET /assets/{id}/similar
    → API fetches asset's AI description
    → Runs BM25 search using description as query (excluding self)
    → Applies adaptive threshold based on corpus size
    → Returns ranked similar assets
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
| AI inference | Moondream | Lightweight, runs locally, no API cost |
| CLI | Python, Click or Typer | Shares models with API server |
| Web UI | React, TypeScript, Tailwind | Required for media grid, virtualized scroll |
| Mac agent | Swift / Electron TBD | Filesystem access, background service |
| Cloud platform | GCP | Employee discounts, known infrastructure |
| Container | Docker Compose (self-hosted), Cloud Run (cloud) | Same image, different orchestration |
| Auth | API keys (v1), Firebase Auth (v2 self-service) | Simple to start, extensible |

---

## 6. Multi-Tenancy Model

### 6.1 Tenant Isolation

Each tenant has a dedicated Postgres database. The control plane routes each API request to the correct database using the API key → tenant_id → connection_string lookup.

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
- Proxies / thumbnails → regenerable from source files via local agent
- Quickwit index → regenerable via search-sync
- AI descriptions → regenerable by re-running vision workers

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
- All endpoints require API key authentication (`Authorization: Bearer {key}`)
- Tenant context derived from API key — never passed as parameter
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
- Core API endpoints (libraries, assets, jobs)
- Local agent CLI (scan, ingest, status)
- Vision worker (Moondream)
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

### Phase 5: Cloud + Self-Service
- Firebase Auth integration
- Self-service signup flow
- Subscription management
- GCP Cloud Run deployment
- Multi-region routing

---

## 11. Self-Hosted Deployment

`docker-compose.yml` includes:
- `api` — FastAPI server
- `postgres` — PostgreSQL 16
- `quickwit` — Search engine
- `worker` — AI vision worker (optional, can run separately)
- `minio` — Object storage (optional, can point at S3/B2)

Configuration via environment variables. No cloud dependencies required. A NAS or $5/month VPS is sufficient for personal use.

---

## 12. What Was Proven in the PoC

The following algorithms and patterns are extracted from the PoC codebase (`media-search`) and reimplemented in the new architecture:

- Video scene segmentation and representative frame extraction
- Moondream integration and vision analysis pipeline
- BM25 similarity search with adaptive threshold
- Lease-based worker job claiming (prevents duplicate processing)
- Quickwit schema and index management
- EXIF extraction, sharpness scoring, face detection
- Proxy format: JPEG (not WebP — cross-platform compatibility)
- Worker base class pattern

The PoC codebase is frozen as a reference. The new codebase is a clean start — no migration path, no backward compatibility burden.
