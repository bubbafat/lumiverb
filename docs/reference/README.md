# /reference — Frozen PoC Algorithm Documentation

These documents capture the proven algorithms, tuned constants, and architectural decisions from the `media-search` PoC (`github.com/bubbafat/media-search`). They are **reference material only** — frozen snapshots to guide reimplementation in the new multi-tenant architecture.

**Do not modify these files.** When an algorithm is superseded in the new codebase, delete the relevant reference doc rather than editing it.

---

## Contents

| File | What it covers |
|------|---------------|
| `video_scene_segmentation.md` | FFmpeg persistent pipe, pHash segmentation, Laplacian best-frame selection, resume, truncation detection, two-stage worker split |
| `worker_base_pattern.md` | `BaseWorker` lifecycle, `FOR UPDATE SKIP LOCKED` lease system, transient vs permanent failure handling, metadata worker phases |
| `bm25_similarity_search.md` | PostgreSQL FTS ranking formula, Quickwit BM25 search, adaptive threshold similarity, multi-index routing, outbox pattern |
| `ai_vision_metadata.md` | Moondream Station API calls, EXIF extraction, sharpness scoring, face detection, visual analysis storage format |

---

## Key Constants to Preserve

| Constant | Value | File | Why |
|----------|-------|------|-----|
| `PHASH_THRESHOLD` | 51 bits | scene_segmenter | Tuned to avoid over-segmentation |
| `TEMPORAL_CEILING_SEC` | 30.0s | scene_segmenter | Prevents infinite scenes in static shots |
| `DEBOUNCE_SEC` | 3.0s | scene_segmenter | Prevents jitter from camera flashes |
| `SKIP_FRAMES_BEST` | 2 | scene_segmenter | Skip transition blur/fade-in frames |
| `OUT_WIDTH` | 480px | video_scanner | Low-res 1 FPS scan stream width |
| `PTS_QUEUE_TIMEOUT` | 10.0s | video_scanner | FFmpeg hung detection |
| `SHARPNESS_MAX_VARIANCE` | 1000.0 | sharpness | Normalization ceiling |
| Face confidence | 0.5 | face_detection | MediaPipe BlazeFace threshold |
| Semantic dedup threshold | 85% | video_worker | rapidfuzz token-ratio merge |
| Tags BM25 boost | ×2 | quickwit_search_repo | Tags appear twice in similarity query |
| Moondream timeout | 120s | moondream_station | Long inference on complex images |

---

## What's NOT Included Here

The following PoC components were intentionally not extracted — either too single-tenant-specific or superseded by the new architecture:

- `src/api/main.py` — monolith API; new architecture is multi-tenant with per-tenant DBs
- `src/api/templates/` — HTML UI; new codebase uses React/TypeScript
- `migrations/` — single-tenant schema; new codebase starts fresh with clean multi-tenant schema
- `src/workers/scanner.py` — filesystem scanner; CLI-side in new architecture
- `src/core/config.py` — single-tenant config; new codebase has separate tenant config model

---

## PoC Repository

`https://github.com/bubbafat/media-search`

Apache 2.0 licensed. Treat as read-only going forward.
