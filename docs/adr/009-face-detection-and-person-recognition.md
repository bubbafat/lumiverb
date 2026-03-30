# ADR-009: Face Detection and Person Recognition

## Status

Proposed

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Schema migration, InsightFace provider, face submission API | Done |
| 2 | CLI integration (ingest + repair), `has_faces` search/browse filter | Done |

## Scope Note

This ADR is **product Phase 2** (Visual Intelligence) work. The `faces`, `people`, and `face_person_matches` tables were created as stubs during Phase 1 with an explicit "do not populate or query" restriction in `docs/cursor-api.md` and `docs/architecture.md`. This ADR lifts that restriction. Phase 1 deliverables include updating those docs to remove the Phase 1 scope fence.

The build phases below (Phase 1, Phase 2) are implementation phases *within* this ADR, not product-level phases.

## Overview

Users cannot filter their libraries by "images that contain people." Face detection and recognition are foundational capabilities that transform a flat file library into a queryable asset graph — "show me all photos with people in them," and eventually "show me all photos of Robert."

This ADR covers face detection using InsightFace (RetinaFace + ArcFace), storing detected faces with bounding boxes and 512-dim face embeddings, integrating detection into the CLI ingest and repair flows, and exposing a `has_faces` search/browse filter. Person clustering, labeling, and recognition UI are out of scope but the schema and embedding storage are designed to support them as the immediate next step.

## Motivation

- The search bar and browse filters have no concept of "people." A photographer with 20,000 images cannot ask "show me photos with faces" without manually tagging.
- Face embeddings are the foundation for person recognition (clustering, labeling, person-based search). Detection must land first.
- InsightFace produces both detection (bounding boxes) and recognition embeddings (512-dim ArcFace) in a single pass — storing both now avoids a costly re-processing step when clustering ships.
- The existing `faces`, `people`, and `face_person_matches` stub tables were created for this purpose but have never been populated or queried.

## Design

### Processing Model

Face detection runs **client-side in the CLI process**, matching the existing pattern for CLIP embeddings and vision AI:

1. **During ingest**: After proxy generation and CLIP embedding, InsightFace runs on the proxy image. Detected faces are POSTed to the API as a separate call after the asset is created.
2. **Backfill via repair**: `lumiverb repair faces` pages through assets with `face_count IS NULL`, downloads each proxy, runs InsightFace, and POSTs results.

InsightFace (buffalo_l model) runs on CPU at ~200ms/image. No GPU required. The `insightface` and `onnxruntime` packages are added to the `cli` dependency extra (client-side processing dependencies), alongside `open-clip-torch`.

### Data Model

**Evolve existing stub tables** via Alembic migration. No tables are dropped.

#### `assets` table — add column:

```sql
ALTER TABLE assets ADD COLUMN face_count INTEGER DEFAULT NULL;
```

- `NULL` — face detection has not run yet
- `0` — detection ran, no faces found
- `N` — N faces detected

This denormalized count enables fast filtering without JOIN/EXISTS on the faces table.

#### `faces` table — add columns:

```sql
ALTER TABLE faces ADD COLUMN detection_model VARCHAR NOT NULL DEFAULT 'insightface';
ALTER TABLE faces ADD COLUMN detection_model_version VARCHAR NOT NULL DEFAULT 'buffalo_l';
```

Existing columns are sufficient: `face_id`, `asset_id`, `bounding_box_json` (JSONB with `{x, y, w, h}` as fractions 0.0–1.0), `embedding_vector` (vector(512) — ArcFace face embedding, unit-normalized), `detection_confidence` (float), `created_at`.

#### `people` table — add columns (for future clustering):

```sql
ALTER TABLE people ADD COLUMN centroid_vector vector(512);
ALTER TABLE people ADD COLUMN confirmation_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE people ADD COLUMN representative_face_id VARCHAR REFERENCES faces(face_id);
```

#### `face_person_matches` table — no changes needed.

#### Index:

```sql
CREATE INDEX ix_faces_asset_id ON faces(asset_id);
CREATE INDEX ix_faces_embedding_hnsw ON faces
  USING hnsw (embedding_vector vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
```

The HNSW index on face embeddings enables fast nearest-neighbor search for future person clustering (DBSCAN, centroid matching). Not used in Phase 1 but created now to avoid a costly index build on a populated table later.

### API Endpoints

#### `POST /v1/assets/{asset_id}/faces` — Submit face detections

Request:
```json
{
  "detection_model": "insightface",
  "detection_model_version": "buffalo_l",
  "faces": [
    {
      "bounding_box": {"x": 0.12, "y": 0.08, "w": 0.15, "h": 0.22},
      "detection_confidence": 0.97,
      "embedding": [0.023, -0.041, ...]
    }
  ]
}
```

Response (201):
```json
{
  "face_count": 2,
  "face_ids": ["face_abc123", "face_def456"]
}
```

Behavior:
- Deletes existing faces for this `(asset_id, detection_model, detection_model_version)` before inserting — idempotent re-detection.
- Sets `assets.face_count` to `len(faces)`. Posting an empty `faces` array sets `face_count = 0` (processed, no faces found).
- Bumps `library.revision` (same pattern as vision submit) so the UI can reflect face_count changes promptly.
- Embedding is optional per face (nullable) — detection without recognition is valid, though InsightFace always produces both.

#### `GET /v1/assets/{asset_id}/faces` — List faces for an asset

Response:
```json
{
  "faces": [
    {
      "face_id": "face_abc123",
      "bounding_box": {"x": 0.12, "y": 0.08, "w": 0.15, "h": 0.22},
      "detection_confidence": 0.97,
      "person": null
    }
  ]
}
```

The `person` field is null until clustering/labeling ships. When it does, it will contain `{"person_id": "...", "display_name": "...", "confirmed": true}`.

#### Search and browse filters

New query parameter on existing endpoints:

- `GET /v1/search?q=sunset&has_faces=true` — only assets with `face_count > 0`
- `GET /v1/browse?has_faces=true` — same for unified browse
- `GET /v1/assets/page?has_faces=true` — same for asset paging

Applied as a Postgres post-filter (`WHERE a.face_count > 0`), consistent with existing filters like `favorite`, `has_rating`, `color`. All queries must operate over `active_assets` (non-trashed, `deleted_at IS NULL`) to respect soft-delete invariants. Not added to Quickwit documents — the filter is fast enough on the indexed integer column.

#### Repair summary

`GET /v1/assets/repair-summary` adds:
```json
{
  "missing_faces": 1234
}
```

Where `missing_faces` = count of assets where `face_count IS NULL`.

### CLI

#### Ingest integration

In `_ingest_image()` (src/cli/ingest.py), after CLIP embedding generation:

```
1. POST /v1/ingest  →  asset created (returns asset_id)
2. Run InsightFace on proxy image
3. POST /v1/assets/{asset_id}/faces  →  faces stored, face_count set
```

Face detection failure does not fail the ingest — it logs a warning and continues. The asset is ingested successfully; faces can be backfilled later via repair.

#### Repair command

```
lumiverb repair faces --library mylib [--concurrency 4] [--dry-run]
```

Follows the exact pattern of `repair embed`:
1. Page through assets with `missing_faces=true` via `/v1/assets/page`
2. Download proxy via `GET /v1/assets/{id}/proxy`
3. Run InsightFace detection locally
4. POST results to `/v1/assets/{id}/faces`
5. Thread pool with configurable concurrency (default 4)
6. Progress bar with ok/fail/skip counters

#### Repair summary display

The repair summary table adds a "Faces" row showing missing_faces count and repair status.

### InsightFace Provider

```
src/workers/faces/insightface_provider.py
```

```python
class InsightFaceProvider:
    model_id = "insightface"
    model_version = "buffalo_l"

    def detect_faces(self, pil_image: Image) -> list[FaceDetection]:
        """Detect faces and generate ArcFace embeddings in one pass.

        Returns list of FaceDetection(bounding_box, confidence, embedding).
        Bounding box coordinates are normalized to 0.0-1.0 fractions of image dimensions.
        Embeddings are 512-dim, L2-normalized.
        """
```

Lazy-loads the InsightFace model on first call (thread-safe, same pattern as CLIPEmbeddingProvider). Uses `insightface.app.FaceAnalysis` with `buffalo_l` model pack. CPU execution via `onnxruntime`.

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| Image with no faces | POST empty faces array → `face_count = 0`. Asset excluded from `has_faces=true` filter. |
| Very small faces (< 5% of image) | InsightFace has a configurable `det_size` threshold. Use default (640x640 detection resolution). Tiny faces may be missed — acceptable. |
| Re-running detection on already-processed asset | Idempotent: old faces deleted, new ones inserted, face_count updated. |
| Proxy image not available | Skip asset, increment `skipped` counter. Logged as warning. |
| InsightFace model not installed | CLI prints clear error: "InsightFace model not found. Run `pip install insightface onnxruntime` or install workers extras." |
| face_count NULL vs 0 | NULL = not yet processed (shows in repair summary). 0 = processed, no faces (does not show in repair). |
| Concurrent detection of same asset | Last write wins (faces are deleted + re-inserted per model). No corruption risk. |
| Asset is trashed after detection starts | POST /faces returns 404. CLI skips, logs warning. |

## Code References

| Area | File | Notes |
|------|------|-------|
| Stub models | `src/models/tenant.py:364-412` | Face, Person, FacePersonMatch — evolve via migration |
| CLIP provider (pattern) | `src/workers/embeddings/clip_provider.py` | InsightFace provider follows same lazy-load pattern |
| Embedding submit endpoint (pattern) | `src/api/routers/assets.py:785-803` | Face submit endpoint follows same pattern |
| Repair embed flow (pattern) | `src/cli/repair.py:80-129` | Face repair follows same download → process → POST pattern |
| Ingest image flow | `src/cli/ingest.py` | Face detection added after CLIP embedding step |
| Search filters | `src/api/routers/search.py:75-99` | `has_faces` added alongside existing filters |
| Browse filters | `src/api/routers/browse.py` | `has_faces` added to browse query |
| Asset page filters | `src/api/routers/assets.py` | `missing_faces` filter for repair paging |
| Repair summary | `src/api/routers/assets.py` | Add `missing_faces` count |
| Similarity endpoint | `src/api/routers/similarity.py` | Existing — face embeddings will enable face-to-face similarity in future |

## Doc References

- `docs/cursor-api.md` — Add face submission endpoint, face list endpoint, has_faces filter docs
- `docs/cursor-cli.md` — Add repair faces command docs
- `docs/architecture.md` — Update processing pipeline to mention face detection
- `docs/reference/ai_face_detection_and_dedup.md` — Existing design spec (this ADR implements detection subset)

## Build Phases

### Requirements

Every phase must satisfy all of the following before it is marked complete:

1. **Tests**: New backend tests for every endpoint and repository method. Edge cases from the table above must be covered as they become relevant. **All tests must pass** — not just new or affected tests, the entire suite (`uv run pytest tests/`). No phase is done until the full suite is clean.
2. **Types**: Frontend TypeScript must compile cleanly (`npx tsc --noEmit`).
3. **Build**: Vite must build without errors (`npx vite build`).
4. **Documentation**: Relevant docs updated to reflect changes in the phase.
5. **Progress**: The phase status table above is updated when a phase completes.
6. **Forward compatibility**: Implementation must read ahead to future phases and ensure data model, API shapes, and component interfaces are set up correctly. If current work reveals changes needed in a future phase, update that phase's description.
7. **Backward compatibility**: If current implementation invalidates or changes assumptions in a previous or future phase, those phases must be updated in this document before the current phase is marked complete.

### Phase 1 — Schema, Provider, and API

**Deliverables:**
- Alembic migration: add `face_count` to assets, add `detection_model`/`detection_model_version` to faces, add `centroid_vector`/`confirmation_count`/`representative_face_id` to people, add HNSW index on `faces.embedding_vector`, add B-tree index on `faces.asset_id`
- `InsightFaceProvider` class at `src/workers/faces/insightface_provider.py` — lazy-load buffalo_l, `detect_faces(pil_image) → list[FaceDetection]`
- `FaceRepository` in `src/repository/tenant.py` — `submit_faces(asset_id, model, version, faces)` (delete + insert + update face_count), `get_by_asset_id(asset_id)`, `count_missing_faces(library_id)`
- `POST /v1/assets/{asset_id}/faces` endpoint
- `GET /v1/assets/{asset_id}/faces` endpoint
- Update `GET /v1/assets/repair-summary` to include `missing_faces`
- Update `GET /v1/assets/page` to accept `missing_faces=true` filter
- Add `insightface` and `onnxruntime` to `cli` dependency extra in pyproject.toml
- Update `docs/cursor-api.md` and `docs/architecture.md` to remove the "do not populate faces/people tables" Phase 1 scope fence
- Tests: fast unit tests for provider (mocked), API endpoint tests, repository tests

**Does NOT include:** CLI integration, search/browse filters, ingest changes.

**Read-ahead:** Phase 2 needs the face submission endpoint and InsightFaceProvider to be stable. The `FaceDetection` dataclass returned by the provider must match the API request schema. The `missing_faces` page filter is needed for the repair command.

**Done when:**
- [ ] All deliverables implemented
- [ ] Tests written and passing (`uv run pytest tests/`)
- [ ] Docs updated (cursor-api.md: new endpoints)
- [ ] Phase status updated above

### Phase 2 — CLI Integration and Search Filter

**Deliverables:**
- Face detection during ingest: `_ingest_image()` runs InsightFace after CLIP, POSTs faces to API. Failure does not block ingest.
- `lumiverb repair faces` command: pages `missing_faces=true`, downloads proxy, detects, POSTs. ThreadPoolExecutor with progress bar.
- Repair summary table displays "Faces" row
- `has_faces=true` query parameter on `GET /v1/search`, `GET /v1/browse`, `GET /v1/assets/page`
- Postgres search fallback supports `has_faces` filter
- Tests: CLI integration tests (mocked API), search/browse filter tests

**Does NOT include:** Person clustering, labeling UI, person-based search, video face detection.

**Read-ahead:** Future ADR for person clustering will use the face embeddings stored in this phase. The HNSW index created in Phase 1 enables DBSCAN neighbor queries. The `people.centroid_vector` column is ready for centroid matching. The `face_person_matches` table is ready for cluster assignment.

**Done when:**
- [ ] All deliverables implemented
- [ ] Tests written and passing (`uv run pytest tests/`)
- [ ] Docs updated (cursor-cli.md: repair faces; cursor-api.md: has_faces filter)
- [ ] Phase status updated above

## Alternatives Considered

**MediaPipe for face detection.** Lighter weight and easier to install, but face embedding quality is significantly worse than ArcFace. Since we're storing embeddings now for use in future clustering, embedding quality matters. InsightFace's bundled detection + recognition in one pass is also more efficient than chaining separate detection and embedding models.

**Server-side processing (inference server on AI box).** Would centralize GPU work but adds operational complexity — another service to deploy and monitor. InsightFace on CPU is fast enough (~200ms/image) for the batch sizes involved. Can be extracted to a server later if needed without changing the API contract.

**Add `has_faces` to Quickwit documents.** Would enable filtering at the search engine level instead of Postgres post-filter. Deferred because: the integer column filter is fast, it avoids search re-sync when face_count changes, and the post-filter pattern is already established for other filters (favorite, color, has_rating).

**Store face crops as files.** The spec describes 160x160 face crops stored alongside proxies. Deferred because crops can be generated on-demand from the proxy image + bounding box coordinates. Avoids storage overhead and simplifies the pipeline. Can be added when the clustering/labeling UI needs face thumbnails.

## What This Does NOT Include

- **Person clustering and labeling** — DBSCAN clustering of face embeddings into person candidates, user labeling flow, confirmation queue. Designed for in schema but not implemented. Expected as the immediate next ADR.
- **Duplicate/derivative detection** — pHash fingerprinting, cluster membership, source nomination. Independent feature, separate ADR.
- **Video face detection** — Multi-frame sampling strategy for video scenes. Requires broader discussion about video processing architecture.
- **Face-based search** — "show me photos of Robert." Requires person clustering to be complete.
- **Privacy controls** — Per-library opt-in/opt-out for face detection. Future consideration.
- **Face crop storage** — Storing extracted face thumbnails. Can be derived from proxy + bounding box on demand.
