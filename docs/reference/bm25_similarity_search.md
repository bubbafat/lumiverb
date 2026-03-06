# Reference: BM25 Similarity Search & Quickwit Integration
> Frozen reference from `media-search` PoC. Do not modify.
> Source: `src/repository/quickwit_search_repo.py`, `src/repository/search_repo.py`, `quickwit/media_scenes_schema.json`

## Architecture: Dual Search Path

The system runs two parallel search implementations that can be swapped via config:

| | PostgreSQL FTS (`search_repo.py`) | Quickwit BM25 (`quickwit_search_repo.py`) |
|---|---|---|
| Used when | `quickwit_enabled = False` or Quickwit unreachable | `quickwit_enabled = True` |
| Index type | GIN index on `to_tsvector('english', visual_analysis)` | Append-optimized inverted index |
| Scale | Good to ~500k assets | Designed for 10M+ documents |
| Updates | In-place (JSONB column) | Append-only (new version on re-analysis) |
| Multi-model | Not supported | Multi-index pattern (one index per model version) |

**Fallback behavior:** If Quickwit is unreachable and `quickwit_fallback_to_postgres = True`, the API falls back silently (logs `WARNING`). If fallback is disabled, returns HTTP 503.

---

## PostgreSQL FTS Search (`search_repo.py`)

### Ranking Formula

```sql
final_rank = base_rank * (1.0 + COALESCE(match_ratio, 0) * 2.0)
```

- `base_rank` = `ts_rank_cd(to_tsvector('english', content), query)`
- `match_ratio` = for videos: `sum(matched_scene_duration) / total_video_duration`; for images: `1.0`
- Videos with many matching scenes rank higher than videos with a single match

### Video Scene Awareness

Search targets `video_scenes.metadata::text` (or OCR subfield) rather than the asset. Per video, the query:
- Finds all matching scenes
- Selects the scene with the highest `scene_rank` as `best_scene_ts` (for deep-link)
- Returns that scene's `rep_frame_path` as the preview image
- Computes `match_ratio` = total matched scene duration / total video duration

### Query Modes

- **Vibe search** (`q=`): targets `visual_analysis::text` (full JSONB) for images; `metadata::text` for scenes
- **OCR search** (`ocr=`): targets only `visual_analysis->>'ocr_text'` for images; `metadata->'moondream'->>'ocr_text'` for scenes
- **Tag filter** (`tag=`): JSONB containment check (`@>`) — bypasses FTS entirely
- Combined `q` + `tag`: FTS first, then tag filter applied as additional WHERE clause

### Query Engine

Uses `websearch_to_tsquery('english', :query)` — supports natural language with implicit AND. Does **not** support explicit boolean operators from users.

---

## Quickwit BM25 Search (`quickwit_search_repo.py`)

### Search Fields (default)
```python
_SEARCH_FIELDS = ["description", "ocr_text", "tags"]
```

### Multi-Index Routing

Each AI model version gets its own Quickwit index:
```
media_scenes_moondream_v2
media_scenes_moondream3_v1
```

The active index per library is stored in `library_model_policy.active_index_name`. The API fetches this on every request (no LRU cache) to stay synchronized across load-balanced workers.

### Document Shape (from schema)

Key searchable fields:
```json
{
  "asset_id": 12345,
  "scene_id": null,          // null for images, set for video scenes
  "library_slug": "my-lib",
  "description": "A person cooking pasta...",
  "ocr_text": "Barilla No. 5",
  "tags": ["cooking", "kitchen", "food"],
  "capture_ts": 1700000000,  // unix timestamp from EXIF
  "has_face": true,
  "sharpness_score": 0.82,
  "camera_make": "Canon",
  "camera_model": "EOS R5",
  "country": "Italy",
  "rep_frame_path": "library/scenes/42.jpg",
  "head_clip_path": "library/clips/42.mp4",
  "scene_start_ts": 14.0,    // seconds into video
  "searchable": true,
  "indexed_at": 1700001000
}
```

Image documents have `scene_id = null`. The type filter distinguishes them:
```python
# Images only:  NOT scene_id:[1 TO *]
# Videos only:  scene_id:[1 TO *]
```

---

## Similarity Search (`find_similar`)

### Adaptive Threshold Algorithm

```python
threshold = min_score
while threshold >= floor:
    results = query(terms, score >= threshold)
    if len(results) >= min_results:
        break
    threshold -= step
return results, threshold_used
```

**Parameters (caller-configured, not hardcoded):**
- `min_score` — starting score threshold
- `floor` — lowest acceptable threshold before giving up
- `step` — decrement per retry
- `min_results` — target result count to stop retrying
- `max_results` — Quickwit `max_hits`

### Query Construction (`_build_similarity_query`)

Given `description` and `tags` from the source asset:
1. Sanitize description: replace all Quickwit special chars (`"()[]{}:^~*?\/+-!&|`) with spaces
2. Tokenize into unique words (order-preserving dedup)
3. Lowercase and dedup tags
4. **Tags appear twice** to increase BM25 weight: `desc_tokens + tag_tokens + tag_tokens`

Example: description "A person cooking pasta", tags ["cooking", "kitchen"]
→ query: `A person cooking pasta cooking kitchen cooking kitchen`

### Score Filtering

Quickwit returns a `score` field with each hit. Score filtering is applied **client-side** after the HTTP response. This is intentional — Quickwit's query-level score filtering syntax is not stable across versions.

### Scope Filters (`_build_scope_filter`)

All scope filters are applied at the Quickwit query level on every attempt:
- `NOT asset_id:<exclude_id>` — always exclude source asset
- `library_slug:<slug>` — library restriction
- `capture_ts:[from TO to]` — date range (unix timestamps)
- `sharpness_score:[min TO *]` — minimum sharpness
- `has_face:true/false` — face presence
- Camera: `(camera_make:Canon AND camera_model:"EOS R5")` — OR across multiple cameras

---

## Quickwit Index Operations

```python
# Create index (reads schema JSON, replaces index_id)
repo.create_index("media_scenes_moondream_v2", "quickwit/media_scenes_schema.json")

# Ingest document (NDJSON format, append-only)
repo.index_document("media_scenes_moondream_v2", doc_dict)

# Drop superseded index (garbage collection)
repo.delete_index("media_scenes_moondream_v1")

# Health check (used before every search, 2s timeout)
repo.is_healthy()  # GET /health/livez
```

**Commit timeout:** `commit_timeout_secs = 10` in schema — documents appear in search within 10s of ingest.

**API base URL:** Configured via `quickwit_url` in `worker_config.yml` (default `http://127.0.0.1:7280`) or `QUICKWIT_URL` env var. Dev/test uses port 7281.

---

## Outbox Pattern (Search Sync)

The `search_sync_queue` table acts as a transactional outbox to prevent split-brain between PostgreSQL and Quickwit:

```sql
-- PostgreSQL triggers write to this table on every asset/scene mutation:
id        SERIAL PK
asset_id  INTEGER
action    ENUM('UPSERT', 'DELETE')
created_at TIMESTAMP
```

The `SearchSyncWorker` claims rows with `FOR UPDATE SKIP LOCKED`, then:
- `UPSERT`: pushes a denormalized document to the active Quickwit index
- `DELETE`: issues HTTP DELETE to Quickwit for the asset's documents

No mutation to `assets` or `video_scenes` should ever bypass this queue.

---

## Quickwit Schema Notes

- `id` field: `tokenizer: raw` (exact match, not analyzed)
- `library_slug`, `camera_make`, `camera_model`, etc.: `tokenizer: raw` (facet/filter fields)
- `description`, `ocr_text`: `tokenizer: default` (BM25 text search)
- `tags`: `array<text>` (default tokenizer)
- `timestamp_field`: `indexed_at` (controls time-based partitioning)
- `fast: true` on numeric/bool fields enables range queries and aggregations

---

## Special Characters

Quickwit query special characters that must be escaped or replaced:
```python
_QUICKWIT_SPECIAL_CHARS = '"()[]{}:^~*?\\/+-!&|'
```
Used in `_sanitize_query` (similarity) and `_escape_term` (scope filters).
