# Lumiverb Visual Intelligence: Duplicate Detection & Person Recognition
## Product Requirements, Design Specification, and Implementation Plan

---

# Part 1: Motivation

## 1.1 The Problem

Creative professionals accumulate large libraries of visual assets over time. A photographer shooting a single event may produce 2,000 raw files, export each at three quality levels, generate web thumbnails, and deliver client-specific crops — resulting in 10,000+ files where the true unique image count is 2,000. A videographer may have the same interview footage transcoded at four resolutions across two projects.

This creates several concrete problems:

- **Storage waste**: Derivative files occupy space without adding unique content
- **Search pollution**: A search returns the same image five times at different sizes
- **No subject index**: There is no way to ask "show me all assets featuring Robert" without manually tagging thousands of files
- **No provenance chain**: It is not clear which file is the original and which are derivatives

These problems compound as library size grows. At 10,000 assets they are annoying. At 100,000 they are paralyzing.

## 1.2 The Opportunity

Two distinct but related capabilities address these problems:

**Duplicate and derivative detection** answers the question: "Are these files the same image?" It groups a raw export and its five derivatives into one logical unit, nominates a source, and lets the rest fade into the background.

**Person recognition** answers the question: "Who is in this image?" It detects faces, groups unknown faces into clusters, lets the user name those clusters once, and then automatically associates every future asset featuring that person.

Together they transform a flat file library into a structured, queryable asset graph.

## 1.3 Why This Is a Premium Feature

Both capabilities require meaningful compute at ingest time — face detection and embedding generation are GPU-accelerated workloads with real cost per asset. The value they provide is proportional to library size; a 50-asset library benefits little while a 50,000-asset library is transformed. The UX investment is also significant — labeling flows, confirmation queues, and person-based search are non-trivial surfaces to build and maintain.

A base product tier handles ingest, proxy generation, and metadata search. These features constitute a Visual Intelligence tier, available as a premium subscription or as a pay-per-library-analysis option.

---

# Part 2: User Experience

## 2.1 Principles

**Best effort, not life or death.** Results improve over time. An image that is not yet clustered is not broken — it simply has not been processed yet. The system makes forward progress continuously in the background without blocking the user.

**The user labels clusters, not individual assets.** The system does the hard work of grouping. The user provides names. This is the core interaction contract for person recognition.

**Derivation is invisible by default.** A user browsing their library should see one canonical image per scene, not five exports of it. The derivatives exist and are accessible but do not pollute the default view.

**Confidence is surfaced, not hidden.** When the system is uncertain — a face match with borderline confidence, a cluster that might be two people — it says so and asks for help rather than silently guessing.

## 2.2 Duplicate and Derivative Detection UX

### 2.2.1 Default Library View

The library grid displays one asset per cluster by default. The displayed asset is the nominated source — the highest-resolution, least-compressed member of the cluster.

A small badge on the asset tile indicates the cluster size: "1 of 5" or a stack icon. Clicking the badge expands an inline drawer showing all cluster members with their dimensions, file size, and format.

### 2.2.2 Cluster Detail View

When a user opens a cluster detail view they see:

- The nominated source displayed prominently
- All derivatives listed below with metadata: dimensions, file size, format, Hamming distance from source
- A "Change source" option allowing the user to manually nominate a different member
- A "Split" option if the user believes the cluster is incorrect — two different images that were incorrectly grouped

### 2.2.3 Source Nomination

The system nominates a source automatically using this priority order:

1. Largest pixel area (width × height)
2. Largest file size among equal-area candidates
3. Presence of EXIF camera metadata (exports often strip it)
4. Earliest creation timestamp

The user can override this at any time. Manual nominations are persisted and survive re-clustering.

### 2.2.4 Processing State

While fingerprinting and clustering are running, assets in the default view are displayed ungrouped. A subtle status indicator in the library header shows "Analyzing library — 12,400 of 25,000 assets processed." No spinner, no blocking modal. The library is fully usable during processing.

## 2.3 Person Recognition UX

### 2.3.1 The People Index

A dedicated People section in the navigation shows a grid of person cards. Each card displays:

- The person's name (if labeled) or "Unknown Person #n" (if not)
- A representative face crop — the highest-confidence embedding from the cluster
- An asset count: "Appears in 847 assets"

Unnamed clusters are shown at the bottom of the grid, sorted by size descending — the largest unnamed clusters are most likely to be worth labeling.

### 2.3.2 Labeling Flow

When a user clicks an unnamed cluster they see:

- A grid of face crops from that cluster
- A name input field at the top
- A "These are all the same person" confirmation button
- Individual crop checkboxes to remove incorrect matches before confirming

The user enters a name and confirms. All assets in the cluster are immediately associated with that person. The cluster's mean embedding becomes that person's centroid.

If a cluster is clearly two different people mixed together, the user can select the crops belonging to one person, click "This is a different person," and split the cluster. The selected crops become a new cluster.

### 2.3.3 Confirmation Queue

As the system processes new assets, matches with moderate confidence (not high enough to auto-associate, not low enough to discard) are placed in a confirmation queue. The user sees a notification: "3 possible matches for Robert — confirm?"

Clicking opens a review view showing each candidate face crop alongside the best matching known crop for Robert. The user confirms or rejects each one. Confirmed matches refine Robert's centroid and confirmed embedding set. Rejected matches are returned to the unknown pool.

### 2.3.4 Person Asset View

Clicking a named person in the People index opens their asset view — a standard library grid filtered to assets featuring that person. All existing sort, filter, and search capabilities apply within this view.

For video assets, a timeline strip shows when the person appears. Each contiguous appearance segment is marked. Clicking a segment opens the video at that timestamp.

### 2.3.5 Search Integration

Person name becomes a first-class search filter. A search for "Robert outdoor 2023" matches assets that feature Robert, were shot outdoors (from scene description), and were created in 2023. Person filters compose naturally with all other filters.

### 2.3.6 Privacy Controls

Person recognition is opt-in at the library level. A library owner must explicitly enable it. Within a shared library, a user can opt out of being recognized — their face embeddings are deleted and no new ones are created. This is surfaced clearly in settings.

---

# Part 3: Technical Design

## 3.1 System Overview

The Visual Intelligence system is composed of four independent processing pipelines that run as background workers, writing results into a shared relational database. Each pipeline consumes a queue of unprocessed items and makes forward progress continuously. No pipeline blocks another. Results are best-effort and improve over time.
```
Assets (stills + video scenes)
        │
        ▼
┌─────────────────┐
│  Fingerprint    │  Compute pHash for every still and video keyframe
│  Worker         │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Cluster        │  Group fingerprints by Hamming distance
│  Worker         │  Nominate sources. Merge and split clusters.
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Detection      │  Detect faces in every still and video keyframe
│  Worker         │  Store bounding boxes and crops.
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Embedding      │  Generate 512-dim vector per face crop
│  Worker         │  Match against known persons. Queue unknowns.
└─────────────────┘
```

## 3.2 Perceptual Hashing

### 3.2.1 Algorithm

pHash (perceptual hash) computes a 64-bit hash from the discrete cosine transform of a resized grayscale version of the image. The hash is robust to:

- Resize and resampling
- JPEG compression artifacts at any quality level
- Minor sharpening, contrast adjustment, or color grading
- Format conversion (JPEG to PNG, etc)

Two images are considered derivatives if their Hamming distance (count of differing bits) is ≤ 10. This threshold is tuned for the derivative detection use case — same scene, different export parameters. It is intentionally tight to avoid false positives between genuinely different images with similar composition.

### 3.2.2 Why pHash and Not Other Hashes

- **MD5/SHA**: Exact file identity only. One JPEG re-save produces a completely different hash.
- **aHash (average hash)**: Faster but less discriminating. Higher false positive rate.
- **dHash (difference hash)**: Good for detecting near-identical images but less robust to color grading.
- **pHash**: Best balance of robustness and discrimination for the derivative detection use case.

### 3.2.3 Limitations

pHash is a global image descriptor. It summarizes the entire image as one value. This means:

- Heavy crops (e.g. 16:9 source exported as square) may exceed the distance threshold even though they share content. These will not be clustered automatically.
- Images with very similar composition but different content (headshots, product photos on white backgrounds) may produce low Hamming distances. The threshold should be kept tight (≤ 10) to mitigate this.
- pHash is not suitable for grouping images by subject identity. That is the job of face embeddings.

### 3.2.4 Postgres-Native Hamming Distance

Rather than maintaining an external BK-tree data structure, Hamming distance search is performed directly in Postgres using bitwise XOR and the `bit_count` function. This eliminates external state, serialization logic, and any risk of the in-memory index diverging from the database.
```sql
SELECT image_id, bit_count(phash # $1) AS distance
FROM images
WHERE bit_count(phash # $1) <= 10
ORDER BY distance ASC;
```

**Important**: a standard btree or hash index on `phash` does not accelerate this query. The expression `bit_count(phash # $1)` is evaluated per row and cannot be pruned by a conventional index. At 100,000 rows this is a sequential scan that completes in well under 10ms because the rows are small and fit in memory. This is acceptable. If corpus size grows to 1M+ rows and query latency becomes a problem, that is the point to evaluate a BK-tree or alternative index structure. Do not optimize prematurely.

A helper function makes the query readable and ensures consistent usage across workers:
```sql
CREATE OR REPLACE FUNCTION hamming_distance(a BIGINT, b BIGINT)
RETURNS INT AS $$
  SELECT bit_count(a # b)::INT;
$$ LANGUAGE SQL IMMUTABLE;
```

## 3.3 Clustering

### 3.3.1 Cluster Invariants

- Every asset belongs to exactly one cluster.
- A cluster has exactly one nominated source.
- Cluster membership is many-to-one: many assets, one cluster.

### 3.3.2 Cluster Operations

**Create**: A new asset with no matches becomes a singleton cluster. It is both the only member and the nominated source.

**Join**: A new asset matching one or more members of a single existing cluster is added to that cluster. Source nomination is re-evaluated.

**Merge**: A new asset matching members of two or more distinct existing clusters causes those clusters to be merged into one. The dominant cluster (by source quality) absorbs the others. All member associations are updated.

**Split** (user-initiated): A user identifies that a cluster contains two distinct images. They select the members that belong together, and those are moved to a new cluster. This is a manual operation only — the system does not split clusters automatically.

### 3.3.3 Source Nomination Algorithm

Within a cluster, the source is the member with the highest score computed as:
```
score = (pixel_area * 10) + (file_size_bytes / 1000) + (has_exif ? 5000 : 0)
```

The pixel area term dominates. File size breaks ties among same-resolution exports. EXIF presence is a strong signal of an original rather than a derivative. Manual user nominations override this score permanently.

### 3.3.4 Cluster Convergence

Because new assets are processed incrementally, a cluster's final state may not be reached immediately. A new asset may be processed before the asset it should be clustered with. This is acceptable — both will be singleton clusters initially, and they will be merged when the second is processed if the first is already in the database with a pHash.

A periodic reconciliation job re-evaluates all singleton clusters against the full corpus to catch missed merges. This runs nightly or on demand.

## 3.4 Face Detection

### 3.4.1 What Detection Produces

For each image (still or video keyframe), detection produces zero or more face records, each containing:

- Bounding box: x, y, width, height as fractions of image dimensions (0.0–1.0)
- Detection confidence: float 0.0–1.0
- A face crop: the bounding box region extracted from the proxy image, resized to a standard 160×160px

Detection is run against proxy images, not source files. Proxy resolution is sufficient for detection and embedding. This avoids the need to access source files after initial proxy generation.

### 3.4.2 Detection Models

Recommended: RetinaFace or MTCNN. Both are well-supported, accurate at a range of face sizes, and run efficiently on CPU for moderate batch sizes (GPU preferred for large backlogs).

Detection is a read-only pass against existing proxy images. It does not modify any existing records. It can be re-run safely at any time.

### 3.4.3 Video Scene Sampling Strategy

Running detection on a single keyframe per scene creates an unacceptable risk: if that frame is motion-blurred, blinked, or partially occluded, the person is missed for the entire scene with no fallback.

Instead, detection uses a variable-density sampling strategy based on scene duration:
```
sample_count = CLAMP(FLOOR(duration_sec / 30), 5, 20)
```

A 3-second cut gets 5 samples. A 10-minute interview gets 20 samples. Samples are taken at evenly spaced intervals across the scene duration.

For each sample frame, the lightweight detector runs and produces a quality score per detected face:
```
quality_score = bbox_area * detection_confidence
```

where `bbox_area` is the fraction of the frame occupied by the bounding box. Only the single highest-scoring crop across all sample frames is forwarded to the embedding worker. This ensures the embedding worker always receives the best available face from the scene without multiplying embedding cost by the sample count.

This approach avoids introducing the concept of "virtual scenes" — the scene record is unchanged. The sampling is an internal implementation detail of the detection worker.

## 3.5 Face Embedding

### 3.5.1 What Embedding Produces

Each face crop is passed through a recognition model producing a 512-dimensional float32 vector. This vector encodes identity — two photos of the same person will have vectors close together in this space regardless of lighting, angle, age within reason, or compression. Two different people will have vectors far apart.

All embeddings are normalized to unit length before storage. This is required for cosine similarity to be computed correctly as a dot product, and for distance thresholds to be consistent across all comparisons.

**Distance vs similarity**: throughout this document, matching thresholds are expressed as **cosine distance** (not cosine similarity). Cosine distance = 1 - cosine similarity. A distance of 0.0 means identical vectors. A distance of 1.0 means completely orthogonal. This convention is used consistently to avoid the silent sign-error bugs that occur when distance and similarity values are mixed.

| Cosine Distance | Cosine Similarity | Interpretation |
|---|---|---|
| ≤ 0.10 | ≥ 0.90 | Strong match — auto-associate |
| 0.10 – 0.30 | 0.70 – 0.90 | Candidate — k-NN fallback then confirm |
| > 0.30 | < 0.70 | No match |

### 3.5.2 Embedding Models

Recommended: ArcFace (InsightFace implementation). It is state of the art for face recognition, well-supported, and produces stable 512-dim embeddings. FaceNet is an acceptable alternative.

### 3.5.3 Storage

Embeddings are stored as `vector(512)` using the pgvector Postgres extension. This enables efficient similarity search directly in the database without a separate vector store.

Storage cost: 512 × 4 bytes = 2KB per embedding. At 100,000 faces this is ~200MB — well within comfortable Postgres territory.

## 3.6 Person Identity

### 3.6.1 Person Record

A person record represents a named or unnamed identity. It contains:

- A name (nullable — unnamed until the user labels it)
- A centroid: the mean of all confirmed embeddings for this person, stored as `vector(512)`, unit-normalized
- A confirmation count: how many embeddings have been confirmed for this person
- A created timestamp

### 3.6.2 Hybrid Centroid Matching

A single mean vector (centroid) is an imperfect representation of a person. Profile views, strong lighting variation, and significant age differences all produce embeddings that may be far from the mean even though they belong to the same person. This causes identity fragmentation — the same person split into multiple unnamed clusters.

The hybrid centroid approach addresses this without the full complexity of per-person multi-centroid clustering. The centroid serves as a fast gatekeeper. When a new embedding falls in the borderline range, a k-nearest-neighbor search against all confirmed embeddings for that person is performed as a fallback.

**Matching algorithm for a new embedding E against a known person P**:

1. Compute cosine distance `d` between E and P's centroid.
2. If `d ≤ 0.10`: auto-associate. No further check needed.
3. If `0.10 < d ≤ 0.30`: query the k nearest confirmed embeddings for P (k = min(20, confirmation_count)) using pgvector. If any confirmed embedding has distance ≤ 0.20 from E, treat as a confident match and auto-associate. Otherwise, add to confirmation queue.
4. If `d > 0.30`: no match against P. Move to next person or unknown pool.

The k-NN fallback catches the profile-vs-frontal and lighting-variation cases without requiring the system to maintain separate centroids per pose or condition. The confirmed embedding set is already stored — this is a query change, not a schema change.

**Centroid update on confirmed association**:
```
new_centroid = normalize(
  (old_centroid * confirmation_count + new_embedding) / (confirmation_count + 1)
)
```

The centroid is renormalized to unit length after each update. As more embeddings are confirmed, the centroid becomes more representative and the k-NN fallback is needed less frequently.

### 3.6.3 Unknown Face Clustering

Before any persons are named, all embeddings are unknown. DBSCAN is run over the full embedding space to form initial clusters.

DBSCAN is chosen because:
- It does not require specifying the number of clusters in advance
- It handles noise (faces that don't cluster with anyone) naturally by marking them as outliers
- It finds clusters of arbitrary shape in high-dimensional space

Parameters:
- `epsilon`: maximum cosine distance between two embeddings to be considered neighbors — start at 0.15
- `min_samples`: minimum embeddings to form a cluster — start at 3

Outliers become singleton unknown persons. They may be merged manually or by future clustering passes as more embeddings accumulate.

### 3.6.4 Centroid Matching for New Assets

When a new face embedding is generated, it is compared against all known person centroids using the hybrid matching algorithm described in 3.6.2:

- Distance ≤ 0.10: auto-associate, no confirmation needed, update centroid
- Distance 0.10–0.30: run k-NN fallback against confirmed embeddings for that person
  - k-NN match found (distance ≤ 0.20): auto-associate, update centroid
  - k-NN no match: add to confirmation queue for that person
- Distance > 0.30 against all known persons: add to unknown pool, include in next DBSCAN pass

### 3.6.5 Asset-Level and Segment-Level Association

**Asset level**: A person is associated with an asset if any face in that asset matches that person at or above the auto-associate threshold, or if the user has manually confirmed the association. This is the primary index used for search and the People asset view.

**Segment level** (video only): Contiguous scenes in which a person appears are collapsed into timestamp ranges. A gap tolerance of 5 seconds is applied — if a person disappears for fewer than 5 seconds between two appearances, the gap is collapsed into one segment. Segments are stored as `(asset_id, person_id, start_sec, end_sec)`. They are computed once after the embedding pass for a video asset and updated when new scenes are added.

Scene-level face associations are an implementation detail and are not surfaced directly in the UI.

---

# Part 4: Database Schema
```sql
-- Core asset table (assumed to exist)
-- images (id, path, proxy_path, width, height, file_size, created_at, ...)
-- video_assets (id, path, proxy_path, duration_sec, ...)
-- video_scenes (id, asset_id, start_sec, end_sec, keyframe_path, ...)

-- Fingerprints
ALTER TABLE images ADD COLUMN phash BIGINT;
ALTER TABLE images ADD COLUMN phash_computed_at TIMESTAMPTZ;
ALTER TABLE video_scenes ADD COLUMN phash BIGINT;
ALTER TABLE video_scenes ADD COLUMN phash_computed_at TIMESTAMPTZ;

-- Hamming distance helper
CREATE OR REPLACE FUNCTION hamming_distance(a BIGINT, b BIGINT)
RETURNS INT AS $$
  SELECT bit_count(a # b)::INT;
$$ LANGUAGE SQL IMMUTABLE;

-- Derivative clusters
CREATE TABLE asset_clusters (
    id          BIGSERIAL PRIMARY KEY,
    source_id   BIGINT NOT NULL REFERENCES images(id),
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE asset_cluster_members (
    cluster_id      BIGINT NOT NULL REFERENCES asset_clusters(id),
    image_id        BIGINT NOT NULL REFERENCES images(id),
    distance        INT NOT NULL DEFAULT 0,
    user_nominated  BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (cluster_id, image_id)
);

CREATE UNIQUE INDEX ON asset_cluster_members(image_id);

-- Face detection
CREATE TABLE face_detections (
    id              BIGSERIAL PRIMARY KEY,
    image_id        BIGINT REFERENCES images(id),
    scene_id        BIGINT REFERENCES video_scenes(id),
    bbox_x          FLOAT NOT NULL,
    bbox_y          FLOAT NOT NULL,
    bbox_w          FLOAT NOT NULL,
    bbox_h          FLOAT NOT NULL,
    confidence      FLOAT NOT NULL,
    quality_score   FLOAT NOT NULL,   -- bbox_area * confidence, used for best-of-N selection
    crop_path       TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    CHECK (
        (image_id IS NOT NULL AND scene_id IS NULL) OR
        (image_id IS NULL AND scene_id IS NOT NULL)
    )
);

-- Face embeddings
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE face_embeddings (
    id              BIGSERIAL PRIMARY KEY,
    detection_id    BIGINT NOT NULL REFERENCES face_detections(id),
    embedding       vector(512) NOT NULL,  -- unit-normalized
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX ON face_embeddings USING ivfflat (embedding vector_cosine_ops);

-- Person identity
CREATE TABLE persons (
    id                  BIGSERIAL PRIMARY KEY,
    name                TEXT,
    centroid            vector(512),       -- unit-normalized mean of confirmed embeddings
    confirmation_count  INT NOT NULL DEFAULT 0,
    opted_out           BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE person_face_associations (
    person_id       BIGINT NOT NULL REFERENCES persons(id),
    embedding_id    BIGINT NOT NULL REFERENCES face_embeddings(id),
    distance        FLOAT NOT NULL,        -- cosine distance at time of association
    confirmed       BOOLEAN NOT NULL DEFAULT false,
    pending         BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (person_id, embedding_id)
);

-- Asset-level person presence
CREATE TABLE asset_persons (
    asset_type  TEXT NOT NULL CHECK (asset_type IN ('image', 'video')),
    asset_id    BIGINT NOT NULL,
    person_id   BIGINT NOT NULL REFERENCES persons(id),
    PRIMARY KEY (asset_type, asset_id, person_id)
);

-- Video segment-level person appearance
CREATE TABLE video_person_segments (
    id          BIGSERIAL PRIMARY KEY,
    asset_id    BIGINT NOT NULL REFERENCES video_assets(id),
    person_id   BIGINT NOT NULL REFERENCES persons(id),
    start_sec   FLOAT NOT NULL,
    end_sec     FLOAT NOT NULL
);

CREATE INDEX ON video_person_segments(asset_id, person_id);
```

---

# Part 5: Phased Implementation Plan

Each phase is self-contained. It has clear inputs, clear outputs, and does not depend on phases that follow it. Each phase can be implemented, tested, and shipped independently.

---

## Phase 1: Fingerprint Schema and Still Image Backfill Worker

**Goal**: Every existing still image has a pHash stored in the database.

**Schema changes**:
- Add `phash BIGINT` column to the images table, nullable, default NULL.
- Add `phash_computed_at TIMESTAMPTZ` column.
- Add the `hamming_distance` SQL function.

**Worker behavior**:
- Query for images where `phash IS NULL`, ordered by `created_at ASC`, in batches of 100.
- For each image, open the proxy file and compute pHash using the `imagehash` Python library: `imagehash.phash(image)`. Convert to integer with `int(str(hash), 16)`.
- Write `phash` and `phash_computed_at = now()` back to the images table.
- Sleep 100ms between batches.
- Log progress every 1000 images: "Fingerprinted N of M images."
- On startup, log how many images remain unfingerprinted.

**Completion criteria**:
- All existing images have a non-null pHash.
- New images written to the database without a pHash are picked up within one worker cycle.
- Worker exits cleanly when queue is empty and restarts on a timer.

**Testing**:
- Unit test: compute pHash for a known image, assert the integer value is stable across two calls.
- Unit test: resize the same image to 50% and assert Hamming distance between the two hashes is ≤ 5.
- Integration test: insert 10 images with null pHash, run worker, assert all 10 have non-null pHash after one cycle.

---

## Phase 2: Video Scene Fingerprinting

**Goal**: Every existing video scene keyframe has a pHash stored in the database.

**Schema changes**:
- Add `phash BIGINT` and `phash_computed_at TIMESTAMPTZ` to the video_scenes table.

**Worker behavior**:
- Identical logic to Phase 1 but queries `video_scenes` where `phash IS NULL` and reads from `keyframe_path`.
- Can extend the Phase 1 worker to alternate between both queues, or run as a separate worker class.

**Completion criteria**:
- All video scene keyframes have a non-null pHash.

**Testing**:
- Same as Phase 1 but against video scene records.

---

## Phase 3: Derivative Cluster Schema and Backfill

**Goal**: Cluster schema is in place and every existing image has a singleton cluster membership.

**Schema changes**:
- Create `asset_clusters` table.
- Create `asset_cluster_members` table with unique index on `image_id`.

**Backfill**:
```sql
WITH new_clusters AS (
    INSERT INTO asset_clusters (source_id)
    SELECT id FROM images
    WHERE id NOT IN (SELECT image_id FROM asset_cluster_members)
    RETURNING id, source_id
)
INSERT INTO asset_cluster_members (cluster_id, image_id, distance)
SELECT id, source_id, 0 FROM new_clusters;
```

**Completion criteria**:
- `SELECT COUNT(*) FROM images` equals `SELECT COUNT(*) FROM asset_cluster_members`.
- Every image has exactly one cluster membership.

---

## Phase 4: Postgres-Native Cluster Worker

**Goal**: Images are grouped into derivative clusters based on pHash Hamming distance. All matching logic runs inside Postgres — no external index, no serialized state.

**Dependencies**: Phases 1 and 3 complete.

**Worker behavior**:

Maintain a `last_processed_cursor` timestamp in a worker state table, initialized to epoch. Each cycle:

1. Query images where `phash_computed_at > last_processed_cursor`, ordered by `phash_computed_at ASC`, limit 500.
2. For each image, run the Hamming distance query:
```sql
SELECT m.cluster_id, i.id AS image_id, i.width, i.height, i.file_size,
       hamming_distance(i.phash, $1) AS distance
FROM images i
JOIN asset_cluster_members m ON m.image_id = i.id
WHERE i.phash IS NOT NULL
  AND hamming_distance(i.phash, $1) <= 10
  AND i.id != $2
ORDER BY distance ASC;
```

3. Collect the distinct cluster IDs from the results.
4. If no matches: the image's existing singleton cluster stands. No action.
5. If all matches are in the same cluster as the current image: no action.
6. If matches are in one other cluster: merge current image's cluster into the matched cluster (see merge operation below).
7. If matches span multiple clusters: merge all of them into the dominant cluster.
8. After processing all images in the batch, advance `last_processed_cursor` to the latest `phash_computed_at` in the batch.

**Merge operation** (single database transaction):

- Compute source quality score for all members across both clusters: `(width * height * 10) + (file_size / 1000)`. Add 5000 if EXIF metadata is present (requires a join to image metadata).
- The cluster whose current source has the higher score is the winner.
- Update all `asset_cluster_members` rows from losing cluster(s) to point to the winning cluster ID.
- Delete losing clusters from `asset_clusters`.
- Re-evaluate source nomination for the merged cluster. Skip if any member has `user_nominated = true` — user nominations are never overridden.

**Source nomination query**:
```sql
UPDATE asset_clusters
SET source_id = (
    SELECT i.id
    FROM asset_cluster_members m
    JOIN images i ON i.id = m.image_id
    WHERE m.cluster_id = $1
    ORDER BY (i.width * i.height) DESC, i.file_size DESC
    LIMIT 1
)
WHERE id = $1
  AND NOT EXISTS (
    SELECT 1 FROM asset_cluster_members
    WHERE cluster_id = $1 AND user_nominated = true
  );
```

**Completion criteria**:
- After a full pass, images with pHash Hamming distance ≤ 10 are in the same cluster.
- Each image belongs to exactly one cluster.
- The nominated source within each cluster is the highest-quality member unless a user nomination exists.
- A full re-run produces no further merges (idempotent).

**Testing**:
- Unit test `hamming_distance`: assert `hamming_distance(0, 1) = 1`, `hamming_distance(0, 3) = 2`, `hamming_distance(x, x) = 0`.
- Integration test: insert 3 images where A and B have distance 4, B and C have distance 6, A and C have distance 8. Assert all three end up in the same cluster after one worker pass.
- Integration test: insert 2 images with distance 15. Assert they remain in separate clusters.
- Integration test: insert one large and one small version of the same image. Assert the large version is nominated as source.
- Integration test: run the worker twice on the same dataset. Assert cluster count is identical after both runs.

---

## Phase 5: Nightly Reconciliation Job

**Goal**: Catch any missed merges from incremental processing.

**Behavior**:
- Query all singleton clusters (clusters with exactly one member).
- For each, run the Hamming distance query against all other images.
- If any match is found outside the singleton's current cluster, merge.
- Log the number of merges performed.

**Completion criteria**:
- After the job runs, no singleton cluster has a pHash within distance 10 of any member of a non-singleton cluster.

---

## Phase 6: Face Detection Schema and Worker

**Goal**: Every still image and video scene has been analyzed for faces using variable-density sampling. Bounding boxes and best-quality crops are stored.

**Schema changes**:
- Create `face_detections` table including `quality_score` column.
- Add `face_detection_processed_at TIMESTAMPTZ` to images table.
- Add `face_detection_processed_at TIMESTAMPTZ` to video_scenes table.

**Worker behavior for still images**:
- Query images where `face_detection_processed_at IS NULL`, batch size 50.
- For each image, run RetinaFace or MTCNN on the proxy file.
- For each detected face, compute `quality_score = bbox_area * confidence` where `bbox_area = bbox_w * bbox_h` (fractional).
- Insert one row per detected face into `face_detections`. Extract and save the 160×160 crop to the configured crops directory.
- Set `face_detection_processed_at = now()` regardless of whether faces were found.

**Worker behavior for video scenes**:
- Query scenes where `face_detection_processed_at IS NULL`, batch size 20 (scenes are more expensive than stills).
- Compute sample count: `CLAMP(FLOOR(duration_sec / 30), 5, 20)`.
- Extract sample frames at evenly spaced intervals across `[start_sec, end_sec]`.
- Run the detector on all sample frames.
- Compute `quality_score = bbox_area * confidence` for every detected face across all sample frames.
- Insert only the single highest-scoring face detection per scene into `face_detections`. Discard all other candidates.
- Set `face_detection_processed_at = now()`.

**Completion criteria**:
- All images and video scenes have `face_detection_processed_at` set.
- All detected faces have rows in `face_detections` with valid bounding boxes, crop paths, and quality scores.
- Images and scenes with no detected faces are still marked as processed.
- Each video scene has at most one row in `face_detections` (the best-quality crop).

**Testing**:
- Unit test: run detection on a known portrait image, assert at least one detection with confidence > 0.9.
- Unit test: run detection on a known landscape image with no people, assert zero detections.
- Unit test: given a 90-second scene, assert sample count is `CLAMP(FLOOR(90/30), 5, 20) = 5`.
- Unit test: given a 600-second scene, assert sample count is `CLAMP(FLOOR(600/30), 5, 20) = 20`.
- Integration test: process 5 images, assert `face_detection_processed_at` is set on all 5.
- Integration test: process a scene with multiple sample frames containing faces at different quality scores, assert only the highest-scoring crop is stored in `face_detections`.

---

## Phase 7: Face Embedding Schema and Worker

**Goal**: Every detected face has a unit-normalized 512-dimensional embedding stored in the database.

**Schema changes**:
- `CREATE EXTENSION IF NOT EXISTS vector;`
- Create `face_embeddings` table with `embedding vector(512)`.
- Create `persons` table.
- Create `person_face_associations` table.

**Worker behavior**:
- Query for face detections with no corresponding row in `face_embeddings`, batch size 100.
- For each detection, load the crop from `crop_path`, run through ArcFace model, produce a 512-dim float32 vector.
- Normalize the vector to unit length: `v = v / ||v||`.
- Insert into `face_embeddings`.

**Completion criteria**:
- Every face detection has a corresponding embedding.
- All embeddings satisfy `|v · v - 1.0| < 1e-6` (unit-normalized).

**Testing**:
- Unit test: embed the same crop twice, assert cosine distance < 0.001 (deterministic).
- Unit test: embed two crops of the same person from different photos, assert cosine distance < 0.20.
- Unit test: embed two crops of clearly different people, assert cosine distance > 0.30.
- Unit test: assert all stored embeddings are unit-normalized.

---

## Phase 8: Unknown Face Clustering

**Goal**: All existing embeddings are grouped into unnamed person clusters using DBSCAN. Person records are created and presented to the user for labeling.

**Scope**: Batch job, not a continuous worker. Runs on demand or nightly.

**Job behavior**:
- Load all embeddings not yet associated with any person.
- Run DBSCAN with `epsilon=0.15` (cosine distance), `min_samples=3`.
- For each cluster produced:
  - Insert a new `persons` row with `name = NULL`.
  - Compute mean of all embeddings in the cluster, normalize to unit length, store as `centroid`.
  - Set `confirmation_count` to the number of embeddings in the cluster.
  - Insert rows into `person_face_associations` with `confirmed = true`, `pending = false`, and `distance` set to each embedding's distance from the centroid.
- For each outlier:
  - Insert a singleton `persons` row with `centroid` set to the single embedding.
  - Insert one `person_face_associations` row with `distance = 0`, `confirmed = true`.
- Log: "Formed N person clusters, M outliers."

**Completion criteria**:
- Every embedding is associated with exactly one person record.
- Person centroids are populated and unit-normalized.
- Running the job twice on the same data produces no changes (idempotent — already-associated embeddings are skipped).

**Testing**:
- Unit test: generate 30 synthetic embeddings (10 per simulated person, well-separated in embedding space). Assert DBSCAN produces exactly 3 clusters.
- Unit test: generate embeddings that are all equidistant from each other (adversarial). Assert the job completes without error, producing singleton outliers.
- Integration test: run the job twice on the same data. Assert person count is unchanged on the second run.

---

## Phase 9: Person Labeling API

**Goal**: API endpoints allow the user to label unknown clusters, split clusters, merge clusters, and confirm or reject pending face associations.

**Endpoints**:

`GET /v1/persons`
- Returns all person records ordered by `confirmation_count DESC`, unnamed persons last.
- Each record includes: id, name, confirmation_count, face_count, asset_count, representative_crop_url.
- `representative_crop_url`: the crop from the face detection whose embedding has the smallest cosine distance to the centroid.

`GET /v1/persons/:id/faces`
- Returns all face crops for this person, paginated.
- Each item: crop_url, detection_id, source asset id, asset type, distance from centroid.

`POST /v1/persons/:id/label`
- Body: `{ "name": "Robert" }`
- Sets the person's name. Returns updated person record.

`POST /v1/persons/:id/remove-faces`
- Body: `{ "detection_ids": [1, 2, 3] }`
- Removes face associations for the specified detections from this person.
- Creates a new unnamed person record containing those faces.
- Recomputes centroids for both persons using the formula in section 3.6.2.
- Returns both updated person records.

`POST /v1/persons/merge`
- Body: `{ "person_ids": [4, 7] }`
- Merges all listed persons into the first. Moves all face associations. Recomputes centroid from all confirmed embeddings. Deletes merged records.

`GET /v1/persons/:id/confirmation-queue`
- Returns pending face associations for this person. Paginated.
- Each item: crop_url, detection_id, source asset id, distance from centroid, distance from nearest confirmed embedding.

`POST /v1/persons/:id/confirm`
- Body: `{ "embedding_ids": [10, 11], "rejected_ids": [12] }`
- Confirmed: set `confirmed = true`, `pending = false`. Update centroid using incremental formula.
- Rejected: delete the association row. Embedding returns to the unknown pool.

**Completion criteria**:
- All endpoints return correct data.
- Label, merge, and split operations correctly recompute centroids.
- Confirmed associations increment `confirmation_count`.
- Rejected associations remove the row entirely and do not leave orphaned pending records.

---

## Phase 10: Live Matching for New Assets

**Goal**: New assets are automatically matched against known person centroids using hybrid centroid matching. Confident matches are auto-associated. Borderline matches enter the confirmation queue.

**Scope**: Extend the Phase 7 embedding worker with matching logic after each embedding is inserted.

**Matching algorithm** (per new embedding E):

1. Query all persons where `centroid IS NOT NULL` and `opted_out = false`, ordered by centroid distance ascending:
```sql
SELECT id, centroid, confirmation_count,
       (embedding <=> centroid) AS distance
FROM persons
WHERE centroid IS NOT NULL AND opted_out = false
ORDER BY distance ASC
LIMIT 1;
```

Note: pgvector's `<=>` operator returns cosine distance directly (1 - cosine similarity) when embeddings are unit-normalized. Confirm unit normalization is enforced in Phase 7 before relying on this.

2. Take the best candidate (smallest distance):
   - `d ≤ 0.10`: auto-associate. Insert into `person_face_associations` with `confirmed = true`, `pending = false`. Update `asset_persons`. Update centroid using incremental formula.
   - `0.10 < d ≤ 0.30`: run k-NN fallback.
   - `d > 0.30`: no match. Add to unknown pool.

3. k-NN fallback for borderline candidates:
```sql
SELECT e.id, (e.embedding <=> $1) AS distance
FROM face_embeddings e
JOIN person_face_associations pfa ON pfa.embedding_id = e.id
WHERE pfa.person_id = $2
  AND pfa.confirmed = true
ORDER BY distance ASC
LIMIT 20;
```

   - If any result has `distance ≤ 0.20`: auto-associate, update centroid, update `asset_persons`.
   - Otherwise: insert into `person_face_associations` with `confirmed = false`, `pending = true`. Do not update `asset_persons` yet.

4. If no person matched at all: the embedding joins the unknown pool. The DBSCAN job (Phase 8) will incorporate it on its next run.

**Asset-level association**:
```sql
INSERT INTO asset_persons (asset_type, asset_id, person_id)
VALUES ($1, $2, $3)
ON CONFLICT DO NOTHING;
```

**Completion criteria**:
- New images with faces matching known persons above the auto-associate threshold produce `asset_persons` rows within one worker cycle.
- Borderline matches appear in the confirmation queue without creating `asset_persons` rows.
- Unmatched embeddings produce no associations and no errors.

**Testing**:
- Integration test: label a person in Phase 9 with 10 confirmed embeddings. Ingest a new image of that person with a frontal crop. Assert `asset_persons` row is created.
- Integration test: ingest an image with a profile crop of the same person that would be borderline on centroid distance but close to a known profile embedding. Assert auto-association via k-NN fallback.
- Integration test: ingest an image of a completely different unlabeled person. Assert no association is created and the embedding appears in the unknown pool.
- Unit test: assert that cosine distance thresholds are applied as distance (not similarity) values. Specifically, assert that an embedding with similarity 0.92 (distance 0.08) triggers auto-associate, and one with similarity 0.88 (distance 0.12) triggers the k-NN fallback.

---

## Phase 11: Video Segment Computation

**Goal**: For each video asset, contiguous timestamp ranges where each known person appears are stored as segment records.

**Scope**: Batch job triggered after the embedding worker completes a pass over a video asset's scenes.

**Job behavior**:
- Accept a `video_asset_id` as input.
- Query all scenes for this asset that have a confirmed face association:
```sql
SELECT vs.start_sec, vs.end_sec, pfa.person_id
FROM video_scenes vs
JOIN face_detections fd ON fd.scene_id = vs.id
JOIN face_embeddings fe ON fe.detection_id = fd.id
JOIN person_face_associations pfa ON pfa.embedding_id = fe.id
WHERE vs.asset_id = $1
  AND pfa.confirmed = true
ORDER BY pfa.person_id, vs.start_sec;
```

- For each person, collect the list of `(start_sec, end_sec)` ranges in chronological order.
- Merge contiguous or near-contiguous ranges: if the gap between `end_sec` of one range and `start_sec` of the next is ≤ 5 seconds, merge them into one segment.
- Delete existing `video_person_segments` rows for this asset.
- Insert merged segments.
- For each person with at least one segment, ensure an `asset_persons` row exists for this video asset.

**Completion criteria**:
- Every confirmed person-video association produces at least one segment row.
- Gaps ≤ 5 seconds between appearances are merged into one segment.
- Segments accurately reflect scene-level appearance timestamps.
- `asset_persons` rows exist for all persons with confirmed segments.

**Testing**:
- Unit test: given appearances at [0–10s], [12–20s], [30–40s] with gap tolerance 5s, assert merge produces [0–20s] and [30–40s].
- Unit test: given appearances at [0–10s] and [16–20s] (gap = 6s), assert they are not merged.
- Integration test: run the job twice on the same asset. Assert segment rows are identical after both runs (idempotent via delete-and-reinsert).

---

## Phase 12: Person Search Integration

**Goal**: Person name is a first-class search filter that composes with all existing filters.

**Scope**: Search API extension only. No new workers or schema changes.

**Behavior**:
- Extend the search endpoint to accept an optional `person_ids: [bigint]` parameter.
- When provided, restrict results to assets present in `asset_persons` for those person IDs using an INNER JOIN or EXISTS subquery.
- Accept an optional `person_name: string` parameter. Resolve to person IDs via:
```sql
SELECT id FROM persons WHERE name ILIKE $1 AND opted_out = false;
```

Then apply as a `person_ids` filter.

- Person filter composes with all existing filters via AND logic. A search for "person_name=Robert AND media_type=video AND year=2023" returns only video assets from 2023 featuring Robert.

**Completion criteria**:
- Search by person name returns only assets with a confirmed `asset_persons` association for that person.
- Combined filters return correctly intersected results.
- Search for a person with no associated assets returns an empty result set, not an error.
- Search for an opted-out person returns an empty result set.

**Testing**:
- Integration test: associate person Robert with 3 assets. Assert search for "Robert" returns exactly those 3 assets.
- Integration test: search for "Robert" with `media_type=video`. Assert only video assets in the result.
- Integration test: search for a person name that does not exist. Assert empty result with 200 status, not a 404 or 500.
- Integration test: mark a person as opted out. Assert search returns no results for that person.