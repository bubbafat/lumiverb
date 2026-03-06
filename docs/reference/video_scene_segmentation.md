# Reference: Video Scene Segmentation
> Frozen reference from `media-search` PoC. Do not modify.
> Source: `src/video/scene_segmenter.py`, `src/video/video_scanner.py`, `src/video/high_res_extractor.py`

## What It Does

Transforms a raw video file into a sequence of semantically meaningful **scenes**, each with a single high-quality representative frame. Unlike frame sampling, it detects actual camera cuts and visual drift.

**Target performance:** 1 hour of 4K video indexed in under 3 minutes (excluding AI inference time).

---

## Stage 1: FFmpeg Persistent Pipe (`VideoScanner`)

A single, long-running FFmpeg process is opened to avoid process-spawn overhead.

**The pipe contract:**
- FFmpeg outputs **raw RGB24 frames at 1 FPS** scaled to **480px width** (even height, aspect-preserving) on `stdout`
- FFmpeg outputs `showinfo` filter metadata on `stderr` asynchronously
- A dedicated `stderr` thread parses `pts_time:` values via regex and puts them on a `Queue`
- For each frame read from `stdout`, the main thread calls `pts_queue.get(timeout=10.0)` — if no PTS arrives within 10 seconds, `SyncError` is raised (FFmpeg hung)
- Frame byte size = `480 * out_height * 3` — computed exactly from source dimensions via `ffprobe` so Python and FFmpeg always agree

**FFmpeg command structure:**
```
ffmpeg -hide_banner -loglevel info [-hwaccel auto] [-ss start_pts]
  -i <input>
  -vf fps=1,scale=480:<out_height>,showinfo
  -f rawvideo -pix_fmt rgb24 pipe:1
```

**Hardware acceleration:** `hwaccel=auto` by default. On retry after truncation failure, software decode is used (`hwaccel=None`).

**Width constant:** `OUT_WIDTH = 480`
**PTS timeout:** `PTS_QUEUE_TIMEOUT = 10.0` seconds

---

## Stage 2: Scene Segmentation (`SceneSegmenter`)

### Tuned Constants
```python
PHASH_THRESHOLD = 51        # Hamming distance bits to trigger new scene
PHASH_HASH_SIZE = 16        # 256-bit pHash
TEMPORAL_CEILING_SEC = 30.0 # Force new scene after 30s
DEBOUNCE_SEC = 3.0          # Ignore triggers within 3s of last cut
SKIP_FRAMES_BEST = 2        # Skip first 2 frames per scene for best-frame selection
```

### Composite Trigger Strategy (`_trigger_keep_reason`)

For each frame, a new scene is triggered when:
1. **Temporal ceiling:** `pts - scene_start_pts >= 30.0` → `SceneKeepReason.temporal`
2. **pHash drift:** `hamming(anchor_phash, frame_phash) > 51` **AND** `elapsed >= 3.0` → `SceneKeepReason.phash`
3. Neither condition → `None` (continue current scene)

The **anchor frame** is the first frame of the current scene. It is replaced only when a new scene starts — not updated during the scene.

**Segmentation version** is computed as `PHASH_THRESHOLD * 10000 + int(DEBOUNCE_SEC * 1000)` = `513000`. Changing either constant invalidates existing scene data automatically.

### Best-Frame Selection (in-scene, real-time)

For every open scene, the sharpest frame is tracked without storing all frames:
- **Metric:** Laplacian variance (`cv2.Laplacian(gray, cv2.CV_64F).var()`)
- **Skip:** First `SKIP_FRAMES_BEST = 2` frames per scene are skipped to avoid transition blur
- The frame with the highest variance `> current_best_sharpness` replaces the stored best
- Only `bytes` + `pts` of the best frame are held in memory

### EOF Handling
- The last open scene is always closed with `SceneKeepReason.forced`
- If `video_duration_sec` is known and exceeds the last decoded PTS, `end_ts` is extended to the full duration to keep the video tail searchable
- If no eligible best frame exists (scene too short), the last decoded frame is used as fallback

---

## Stage 3: High-Resolution Frame Extraction (`high_res_extractor`)

After a scene closes, the best frame's PTS is used to extract a **full-resolution** version:

```
ffmpeg -ss [target_pts - 0.5] -t 1.0 -i <input>
  -vf fps=30,showinfo
  -f image2pipe -vcodec mjpeg -
```

- Runs at 30 FPS in a 1-second window to maximize chance of hitting the exact frame
- MJPEG frames are parsed from stdout via SOI (`\xFF\xD8`) / EOI (`\xFF\xD9`) byte markers
- PTS values are parsed from stderr in parallel via `showinfo`
- Frame paired by index order (FFmpeg emits one showinfo line per frame)
- The frame with PTS **closest to `target_pts`** is selected

---

## Stage 4: Resumable State

The pipeline survives crashes via two PostgreSQL tables:

- **`video_scenes`** — finalized scene rows (`start_ts`, `end_ts`, `rep_frame_path`, AI description)
- **`video_active_state`** — the in-progress scene's anchor pHash, start time, and best-frame sharpness

**Resume flow:**
1. Query `max(end_ts)` from `video_scenes` for this asset
2. Seek FFmpeg pipe to `max_end_ts - 2.0` (2-second overlap)
3. Restore `anchor_phash` and `scene_start_pts` from `video_active_state`
4. Discard frames until `pts >= max_end_ts`, then resume normally

---

## Stage 5: Truncation Detection

The Video Proxy Worker verifies that `max(end_ts)` from the DB is within range of the video's duration (obtained via `ffprobe`). If the decoder stopped early, a `ValueError` is raised: `"Video index truncated: indexed to Xs but duration is Ys"`. The asset is set to `failed` (not `poisoned`) so another worker can retry with software decode.

---

## Two-Stage Worker Split

Scene indexing is intentionally split into two separate workers to separate network I/O from GPU work:

**Video Proxy Worker** (`proxied` → `proxied` with scene data):
1. Transcodes source to a temporary 720p H.264 file (single source read)
2. Extracts thumbnail (frame at 0.0s) and 10-second head-clip (stream copy)
3. Runs scene detection (pHash + best-frame) from the temp file
4. Persists scene bounds and rep frame paths to DB
5. Deletes temp file

**Video Worker** (`proxied` with scene data → `completed`):
1. Loads existing `rep_frame_path` images from disk
2. Runs Moondream vision analysis only
3. Updates scene descriptions in DB — does **not** re-read the source video

---

## Semantic Deduplication (AI Stage)

After Moondream generates a scene description, `rapidfuzz` token-ratio is used to compare it against the previous scene. If similarity > **85%**, the scenes are merged to keep the search index clean.

---

## Dependencies
```
ffmpeg, ffprobe  (system)
imagehash        (pHash)
cv2 / opencv     (Laplacian variance, color conversion)
numpy            (frame buffer manipulation)
PIL / Pillow     (imagehash input)
rapidfuzz        (semantic dedup, threshold=85)
```
