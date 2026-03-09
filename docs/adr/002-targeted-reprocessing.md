
# ADR-002: Targeted Reprocessing, Asset Filter Specs, and Model Provenance

## Status
Accepted

## Context

As Lumiverb matures, several operational needs have emerged that require
re-running workers on subsets of assets rather than entire libraries:

- Upgrading an AI model (Moondream 2 → 3) and rebuilding descriptions/tags
- Repairing corrupt or missing proxy/thumbnail files
- Rebuilding a specific folder after adding new files
- Re-running a single asset from the UI ("rebuild description")
- Re-processing assets from a specific camera model after EXIF worker updates

These are all variations of the same operation: **select a subset of assets
by some predicate, then enqueue a specific job type for them.**

Additionally, AI-generated content requires provenance tracking so that:
- We know which model produced which output
- We can serve results from an old model while building a new one
- We can retire old model outputs once the new index reaches full coverage

---

## Decisions

### 1. Asset Filter Spec

All targeted reprocessing operations are expressed as an **AssetFilterSpec**
— a structured predicate that maps to a SQL WHERE clause server-side.
```python
class AssetFilterSpec(BaseModel):
    # Scope
    library_id: str                        # required
    asset_id: str | None = None            # single asset (overrides all other filters)
    
    # Path filters
    path_prefix: str | None = None         # rel_path LIKE 'path_prefix/%'
    path_exact: str | None = None          # rel_path = 'exact/path/file.jpg'
    
    # Time filters
    mtime_after: datetime | None = None    # file_mtime >= mtime_after
    mtime_before: datetime | None = None   # file_mtime <= mtime_before
    
    # EXIF/metadata filters (available after EXIF worker runs)
    camera_make: str | None = None         # exif->>'camera_make' ILIKE value
    camera_model: str | None = None        # exif->>'camera_model' ILIKE value
    
    # Status filters
    missing_proxy: bool = False            # proxy_key IS NULL
    missing_thumbnail: bool = False        # thumbnail_key IS NULL
    missing_ai: bool = False               # no ai metadata for current model
    
    # Model provenance filter
    ai_model_id: str | None = None         # re-run for assets processed by model
    ai_model_version_lt: str | None = None # re-run if model version < this
```

**Resolution order:** If `asset_id` is set, all other filters are ignored —
it targets exactly one asset. Otherwise filters are combined with AND.

**Single asset from UI:** The right-click "rebuild description" flow sends:
```json
{"library_id": "lib_...", "asset_id": "ast_..."}
```
One asset, one job, one API call.

**Path prefix:** Matches any asset whose rel_path starts with the prefix.
Both folder (`B/2025/June`) and exact file (`B/2025/June/IMG_001.jpg`) are
supported via `path_prefix` vs `path_exact`.

**Camera filter:** Requires EXIF worker to have run. Filtered server-side
against the asset_metadata JSONB column. ILIKE for case-insensitive match.

---

### 2. Enqueue API with Filter Spec
```
POST /v1/jobs/enqueue
Body: {
  "job_type": "proxy" | "thumbnail" | "ai_vision" | "exif" | ...,
  "filter": AssetFilterSpec,
  "force": false
}
Returns: {"enqueued": N}
```

**force=false (default):** Only enqueues assets that don't already have a
pending/claimed job of this type AND haven't been successfully processed
(status != 'complete' for that job type).

**force=true:** Enqueues regardless of existing jobs or completion status.
Cancels any existing pending/claimed jobs for the same asset+job_type before
inserting new ones. Used for rebuild/repair operations.

Server translates AssetFilterSpec to a SQL WHERE clause and bulk-inserts
matching jobs in a single transaction (same batched INSERT pattern as
enqueue_proxy_jobs).

---

### 3. CLI Filter Flags

All worker and enqueue commands accept a consistent set of filter flags:
```bash
# Single asset
lumiverb jobs enqueue --library Test --job-type proxy \
  --asset ast_01KK...

# Path prefix (folder)
lumiverb jobs enqueue --library Test --job-type proxy \
  --path B/2025/June --force

# Exact file
lumiverb jobs enqueue --library Test --job-type proxy \
  --path B/2025/June/IMG_001.jpg --force

# Camera model
lumiverb jobs enqueue --library Test --job-type ai_vision \
  --camera-model "Sony A7 IV" --force

# Date range
lumiverb jobs enqueue --library Test --job-type exif \
  --since 2025-06-01 --until 2025-06-30

# Missing proxies only
lumiverb jobs enqueue --library Test --job-type proxy \
  --missing-proxy

# Rebuild all for model upgrade
lumiverb jobs enqueue --library Test --job-type ai_vision \
  --model-version-lt moondream3 --force
```

Flags map 1:1 to AssetFilterSpec fields. The CLI constructs the spec and
POSTs to /v1/jobs/enqueue.

---

### 4. Model Provenance

Every AI-generated output is stamped with its origin.

**asset_metadata table:**
```sql
CREATE TABLE asset_metadata (
    metadata_id     TEXT PRIMARY KEY,        -- meta_01...
    asset_id        TEXT NOT NULL REFERENCES assets(asset_id),
    model_id        TEXT NOT NULL,           -- "moondream", "clip", "blaze_face"
    model_version   TEXT NOT NULL,           -- "2.0", "3.0", "1.5"
    generated_at    TIMESTAMPTZ NOT NULL,
    data            JSONB NOT NULL,          -- {description, tags, embeddings, ...}
    UNIQUE (asset_id, model_id, model_version)
);
```

When a model is upgraded, the old row is NOT deleted — a new row is inserted
with the new model_version. Both exist simultaneously.

**Querying during transition:** The search layer queries by
`(model_id, model_version)` explicitly. While moondream3 is being built,
queries fan out to both versions and merge — moondream3 results take
precedence when available, moondream2 fills the gap.

**Coverage tracking:**
```sql
-- How complete is moondream3 for this library?
SELECT 
    COUNT(*) FILTER (WHERE m.model_version = '3.0') as v3_count,
    COUNT(*) as total
FROM assets a
LEFT JOIN asset_metadata m ON m.asset_id = a.asset_id 
    AND m.model_id = 'moondream'
WHERE a.library_id = :library_id;
```

Once v3 coverage reaches 100%, v2 rows can be archived or deleted via:
```bash
lumiverb models retire --library Test --model moondream --version 2.0
```

---

### 5. Quickwit Index Naming

Quickwit indexes are named to include model identity:
```
lumiverb_{library_id}_{model_id}_{model_version}
```

Example:
```
lumiverb_lib01KK_moondream_2.0
lumiverb_lib01KK_moondream_3.0
```

During a model transition both indexes are queried. The search API accepts
an optional `model_version` parameter; if omitted it queries the latest
complete index with fallback to the previous version for uncovered assets.

Retiring a model version means:
1. Deleting the Quickwit index
2. Deleting asset_metadata rows for that version
3. Removing the version from the active index routing table

---

### 6. Repair Operations

Common repair scenarios and their CLI commands:

| Scenario | Command |
|---|---|
| Proxy files deleted from disk | `lumiverb jobs enqueue --library X --job-type proxy --missing-proxy` |
| Rebuild all proxies | `lumiverb jobs enqueue --library X --job-type proxy --force` |
| Rebuild folder | `lumiverb jobs enqueue --library X --job-type proxy --path B/2025 --force` |
| Rebuild one file | `lumiverb jobs enqueue --library X --job-type proxy --path B/IMG.jpg --force` |
| Rebuild one asset from UI | `POST /v1/jobs/enqueue {asset_id: "ast_...", job_type: "proxy", force: true}` |
| Upgrade AI model | `lumiverb jobs enqueue --library X --job-type ai_vision --model-version-lt 3.0 --force` |
| Re-run EXIF for camera | `lumiverb jobs enqueue --library X --job-type exif --camera-model "Sony A7 IV" --force` |

---

## Consequences

**Positive:**
- Single unified API for all reprocessing operations
- UI right-click rebuild works via asset_id filter
- Model upgrades are zero-downtime — old results serve while new builds
- Provenance enables audit trail ("when was this description generated")
- Generic filter spec composes — any combination of filters works

**Negative:**
- AssetFilterSpec SQL translation adds complexity to enqueue endpoint
- EXIF-based filters (camera_make, camera_model) require EXIF worker to
  have run first — partial data during initial setup
- Parallel Quickwit indexes during model transitions increase storage and
  query fan-out cost temporarily

## Implementation Order

1. **Now (Phase 1):** Add `force` flag to existing enqueue; add
   `asset_id` and `path_prefix`/`path_exact` filters — these need no
   EXIF data and unblock repair/rebuild workflows immediately.
2. **Step 7 (EXIF worker):** Add `camera_make`, `camera_model` filters
   once metadata is available.
3. **Step 8 (AI vision):** Add `asset_metadata` table, provenance
   stamping, `missing_ai` and `model_version_lt` filters.
4. **Step 9 (Search):** Implement parallel Quickwit index fan-out and
   model transition logic.