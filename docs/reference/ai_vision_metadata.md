# Reference: AI Vision Integration & Metadata Extraction
> Frozen reference from `media-search` PoC. Do not modify.
> Source: `src/ai/vision_moondream_station.py`, `src/metadata/exif_adapter.py`, `src/metadata/sharpness.py`, `src/metadata/face_detection.py`

## Moondream Station Integration

The system calls a **locally running Moondream Station server** — all inference runs in a separate process. No model weights or ML code live in this codebase.

### Endpoint
```
Default: http://localhost:2020/v1
Override: MEDIASEARCH_MOONDREAM_STATION_ENDPOINT env var
```

### Connection Pooling
A persistent `requests.Session` with a single `HTTPAdapter` is reused across all calls:
```python
HTTPAdapter(pool_connections=10, pool_maxsize=20)
```
This avoids TCP socket exhaustion when multiple workers hit the same Station instance.

### Three API Calls Per Image

**1. Caption (description):**
```python
POST /v1/caption
{"image_url": "data:image/jpeg;base64,...", "length": "short", "stream": false}
# Response: {"caption": "A person cooking pasta..."}
```
Fallback: If response lacks `"caption"` key (some model versions return non-standard format), falls back to `/v1/query` with prompt: `"Describe this image briefly in one or two sentences."`

**2. Tags:**
```python
POST /v1/query
{"image_url": "...", "question": "Provide a comma-separated list of single-word tags for this image.", "stream": false}
# Response: {"answer": "cooking, kitchen, food, pasta"}
```
Parsed with order-preserving deduplication: `dict.fromkeys(t.strip() for t in tags_str.split(",") if t.strip())`

**3. OCR:**
```python
POST /v1/query
{"image_url": "...", "question": "Extract all readable text. If there is no text, reply 'None'.", "stream": false}
# Response: {"answer": "Barilla No. 5"}
```
If response is `"None"` (case-insensitive), stored as `null`.

### Image Encoding
All images are sent as base64 JPEG data URLs at **quality 95**. Non-RGB images are converted to RGB before encoding.

### Timeout
Each HTTP call has a **120-second timeout**. For large or complex images, Moondream inference can be slow.

### Error Handling
- `ConnectionError` / `Timeout` → raises `MoondreamUnavailableError` (transient — worker resets asset for retry)
- Other `RequestException` → raises `RuntimeError` (permanent — asset may be poisoned)

### Tiered Analysis (Light / Full)

| Mode | Calls made | Asset status after |
|------|-----------|-------------------|
| `light` | caption + tags (no OCR) | `analyzed_light` |
| `full` | OCR only (appended to existing light data) | `completed` |

The full mode uses **strict merge**: fetch current DB state first, then add only `ocr_text`. Never overwrites description or tags from a prior light pass. If model IDs don't match, re-runs the full light pass instead of merging.

---

## EXIF Extraction (`exif_adapter.py`)

### Long-Lived Process
`exiftool` is started once per worker process and kept alive. Repeated calls use `execute_json` (newer pyexiftool) or `get_metadata_batch` (older):
```python
tool.execute_json("-json", "-n", str(path))
```
The `-n` flag returns numeric values rather than formatted strings (e.g. GPS as decimal degrees, not `DMS`).

### Filtering
Two filtering passes prevent `raw_exif` from growing unbounded:
1. Drop any key starting with `MakerNote` or `MakerNotes` (vendor camera data, can be MB-sized)
2. Drop keys in `VENDOR_FIELD_DENYLIST` (configurable, empty by default)

The `SourceFile` key from exiftool's own output is always removed.

### Error Handling
`ExifToolError` is raised on:
- exiftool process startup failure
- Empty or non-list response
- Response contains non-dict items
- Any `subprocess`-level exception

EXIF failures are logged as `ERROR` but do not poison the asset — the metadata worker leaves the asset in `exif_processing` for manual recovery.

---

## Sharpness Scoring (`sharpness.py`)

```python
def compute_sharpness_from_array(img_bgr: np.ndarray) -> float:
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    laplacian = cv2.Laplacian(gray, cv2.CV_64F, ksize=3)
    variance = float(laplacian.var())
    normalized = min(1.0, variance / SHARPNESS_MAX_VARIANCE)
    return max(0.0, normalized)
```

**Constants:**
- `SHARPNESS_MAX_VARIANCE = 1000.0` — practical ceiling; images above this are all considered "sharp"
- `ksize=3` — 3×3 Laplacian kernel

**Input:** Thumbnail (400px max), not proxy or source. This is fast and consistent.

**Output:** Float `[0.0, 1.0]`. Values near 0 = blurry. Values near 1 = sharp.

**In video segmentation:** The raw (unnormalized) Laplacian variance is used directly for comparison within a scene (not normalized) — only the relative ordering matters.

---

## Face Detection (`face_detection.py`)

Uses **MediaPipe BlazeFace** (short range model) via the Tasks Python API:

```python
FaceDetectorOptions(
    base_options=BaseOptions(model_asset_path="...blaze_face_short_range.tflite"),
    running_mode=VisionTaskRunningMode.IMAGE,
    min_detection_confidence=0.5,
)
```

**Model download:** Cached at `~/.cache/media_search/blaze_face_short_range.tflite`. Downloaded automatically from Google's storage on first use.

**Singleton:** One detector instance per process (lazy init).

**Input:** BGR image (from `cv2.imread`), converted to RGB before passing to MediaPipe.

**Output:** `(has_face: bool, face_count: int)`

**Stored fields:** `has_face` (bool), `face_count` (int) — both searchable in Quickwit.

**Note:** Face detection in the PoC only tells you *whether* faces are present. Face embeddings for identity/clustering (pgvector) are Phase 2 work.

---

## Visual Analysis Storage

All AI output is stored as JSONB in `asset.visual_analysis`:

```json
{
  "description": "A person cooking pasta in a kitchen",
  "tags": ["cooking", "kitchen", "food", "pasta"],
  "ocr_text": "Barilla No. 5",
  "moondream": {
    "description": "...",
    "tags": ["..."],
    "ocr_text": "..."
  }
}
```

For video scenes, stored in `video_scenes.metadata`:
```json
{
  "moondream": {
    "description": "...",
    "tags": ["..."],
    "ocr_text": "..."
  }
}
```

The model name/version is tracked separately in `asset.tags_model_id` and `asset.analysis_model_id` (foreign keys to `aimodel` table).
