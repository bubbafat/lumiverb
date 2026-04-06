# ADR-013: Video Transcription

## Status

Accepted — all phases complete

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Whisper engine + `transcribe` enrich job type + `has_transcript` flag | Complete |
| 2 | Transcript search (new hit type, SRT-aware snippet extraction, Quickwit index) | Complete |
| 3 | UI: download SRT, upload-over-existing, search result rendering | Complete |

## Overview

Lumiverb already supports manual SRT upload (`POST /v1/assets/{id}/transcript`), stores transcript text alongside assets, indexes it in Quickwit for full-text search, and displays it in the Lightbox via `TranscriptViewer`. What's missing is the actual speech-to-text engine and a search experience that returns *where* in a video a phrase was spoken.

This ADR adds automatic transcription via `faster-whisper` as a new enrich job type, a `"transcript"` search hit type that includes timestamps and text snippets, and UI improvements for downloading/uploading SRT files.

After this change, `lumiverb enrich --job-type transcribe` extracts audio from video files, runs Whisper speech-to-text, and submits the SRT to the existing transcript endpoint. Search results for transcript matches include `start_ms`/`end_ms` so clients can generate preview clips locally. The server remains ignorant of client-side preview generation.

## Motivation

- **Search gap**: A 3-hour interview contains hours of searchable dialogue, but without transcription it's invisible to search. Scene descriptions (AI-generated from rep frames) only capture what's *visible*, not what's *said*.
- **Manual upload is tedious**: The current workflow requires external transcription → download SRT → upload to Lumiverb. Automating this removes the friction.
- **Native client preparation**: A NAS-connected native client needs timestamp-level search results to generate N-second preview clips locally. The server provides `asset_id` + `rel_path` + `start_ms`/`end_ms` + `snippet` — the client handles the rest.
- **Correction workflow**: Auto-transcription will have errors. Users need to download the SRT, fix it in a text editor, and re-upload. The existing upload/replace API supports this; the UI needs download and upload-over-existing buttons.

## Design

### Transcription Engine

**`faster-whisper`** (CTranslate2 backend) — 4x faster than OpenAI's original Whisper implementation, lower memory usage, same model weights.

**Audio extraction pipeline:**
```
video file → ffmpeg → 16kHz mono WAV → faster-whisper → SRT segments → POST /v1/assets/{id}/transcript
```

ffmpeg extracts audio without decoding video frames (`-vn`), downsamples to 16kHz mono (`-ar 16000 -ac 1`). A 3-hour 4K video produces ~170MB of 16kHz WAV in seconds. Whisper expects 16kHz mono — anything higher is wasted bandwidth.

**Model selection:**
- Default: `small` (460MB, good accuracy-to-speed tradeoff)
- Configurable via `CLIConfig.whisper_model` (options: `tiny`, `base`, `small`, `medium`, `large-v3`)
- GPU acceleration: uses CUDA if available via CTranslate2, falls back to CPU
- A 3-hour video takes ~2 minutes on GPU (small model), ~15 minutes on CPU

**Voice Activity Detection (VAD):**
`faster-whisper` includes Silero VAD built-in (`vad_filter=True`). This is critical — without VAD, Whisper hallucinates text on silent or music-only videos. A 10-second bird video with no speech produces an empty transcript, not phantom dialogue.

**Language detection:**
Whisper auto-detects language from the first 30 seconds. Stored in `transcript_language`. Multi-language videos (interview switches between English and Spanish) get patchy results — Whisper commits to one language per segment. Acceptable limitation for v1.

**Subprocess isolation:**
Whisper inference runs in a subprocess to ensure bounded memory and a clean GPU context per job. CTranslate2 does not have the ONNX Runtime memory leak, but subprocess isolation still prevents any gradual accumulation (model weights, CUDA contexts, intermediate buffers) across a batch of hundreds of videos. The `_transcribe_one()` function extracts audio to a temp file, then spawns a subprocess that loads the model, transcribes, and exits. This matches the existing face detection subprocess pattern.

### Data Model

**New column on `assets` table:**

| Column | Type | Default | Purpose |
|--------|------|---------|---------|
| `has_transcript` | `boolean` | `NULL` | Three-state: `NULL` = never attempted, `false` = attempted and permanently empty, `true` = has transcript |

**`has_transcript = false` semantics:** Set when transcription ran and produced no usable result. This covers two deterministic cases: (1) VAD detected no speech (silent/music-only video), and (2) ffmpeg found no audio track. Neither will produce a different result on retry, so `false` means "don't retry." Transient failures (OOM, ffmpeg crash, disk full) cause `_transcribe_one()` to return `None`, which leaves `has_transcript = NULL` — the asset will be retried on the next enrich run.

**Existing columns (no changes):**
- `transcript_srt` — full SRT content
- `transcript_text` — plain text for search indexing
- `transcript_language` — auto-detected or user-specified
- `transcribed_at` — timestamp

**Migration:** Add `has_transcript` column. Backfill from existing data: `transcript_srt IS NOT NULL` → `true`, `transcribed_at IS NOT NULL AND transcript_srt IS NULL` → `false` (edge case: deleted transcript), else `NULL`. Drop and recreate `active_assets` view.

**New MISSING_CONDITIONS entry:**
```python
"missing_transcription": (
    "a.has_transcript IS NULL"
    " AND a.media_type = 'video'"
    " AND a.duration_sec IS NOT NULL"
)
```

The `duration_sec IS NOT NULL` guard ensures we only attempt transcription on videos that completed scan (which populates duration via ffprobe). Videos without duration are either still being scanned or had a scan failure — transcription would fail too. This matches the existing `missing_video_scenes` condition.

### Quickwit: Transcript Index

Transcript segments need their own Quickwit index for timestamp-level search. This follows the scene index pattern.

**New index:** `lumiverb_{tenant_id}_transcripts`

**Schema (`quickwit/transcript_index_schema.json`):**
```json
{
  "version": "0.8",
  "doc_mapping": {
    "field_mappings": [
      {"name": "id", "type": "text", "tokenizer": "raw", "indexed": true, "stored": true},
      {"name": "asset_id", "type": "text", "tokenizer": "raw", "indexed": true, "stored": true, "fast": true},
      {"name": "library_id", "type": "text", "tokenizer": "raw", "indexed": true, "stored": true, "fast": true},
      {"name": "rel_path", "type": "text", "tokenizer": "default", "indexed": true, "stored": true, "record": "basic"},
      {"name": "media_type", "type": "text", "tokenizer": "raw", "indexed": true, "stored": true, "fast": true},
      {"name": "start_ms", "type": "i64", "indexed": true, "stored": true, "fast": true},
      {"name": "end_ms", "type": "i64", "indexed": true, "stored": true, "fast": true},
      {"name": "text", "type": "text", "tokenizer": "default", "indexed": true, "stored": true, "record": "position", "fieldnorms": true},
      {"name": "language", "type": "text", "tokenizer": "raw", "indexed": true, "stored": true, "fast": true},
      {"name": "indexed_at", "type": "datetime", "indexed": true, "stored": true, "fast": true, "input_formats": ["unix_timestamp"], "precision": "seconds"}
    ],
    "timestamp_field": "indexed_at"
  },
  "search_settings": {
    "default_search_fields": ["text"]
  },
  "indexing_settings": {
    "commit_timeout_secs": 10
  }
}
```

Each SRT segment becomes one Quickwit document. A 3-hour video with ~1,000 SRT segments produces 1,000 documents. This is small relative to Quickwit's capacity.

**Document builder:**
```python
def build_transcript_document(asset: Asset, segment: SrtSegment) -> dict:
    return {
        "id": f"{asset.asset_id}_{segment.start_ms}_{segment.end_ms}",
        "asset_id": asset.asset_id,
        "library_id": asset.library_id,
        "rel_path": asset.rel_path,
        "media_type": asset.media_type,
        "start_ms": segment.start_ms,
        "end_ms": segment.end_ms,
        "text": segment.text,
        "language": asset.transcript_language or "",
        "indexed_at": int(utcnow().timestamp()),
    }
```

Document IDs use `{asset_id}_{start_ms}_{end_ms}` rather than SRT sequence numbers, which are not guaranteed stable or unique across user-edited SRT files.

### SRT Segment Parsing

New utility in `src/core/srt.py`:

```python
@dataclass
class SrtSegment:
    index: int
    start_ms: int
    end_ms: int
    text: str

def parse_srt_segments(srt_content: str) -> list[SrtSegment]:
    """Parse SRT into structured segments with millisecond timestamps."""
```

This is needed for:
1. Building per-segment Quickwit documents
2. Extracting snippets from search results (map timestamp → text)

### API Changes

#### Existing endpoint changes

**`POST /v1/assets/{id}/transcript`** — Side effects by phase:

*Phase 1:*
- Set `has_transcript = True` if SRT is non-empty, `False` if empty
- Clear `search_synced_at` on the asset (so the sweep re-indexes the `transcript_text` field in the asset index)
- Existing behavior unchanged: store SRT, extract plain text, sync to search

*Phase 2 adds:*
- Parse SRT into segments, delete old segment documents from the transcript Quickwit index, ingest new segment documents. Deletion is scoped by tenant index + `asset_id` field: `delete_tenant_transcript_documents(tenant_id, asset_id)` issues a Quickwit delete-by-query (`asset_id:{asset_id}`) against the tenant's transcript index, then ingests the new segments. This is the same pattern used for scene index management.

**`DELETE /v1/assets/{id}/transcript`** — Side effects by phase:

*Phase 1:*
- Set `has_transcript = False` (not `NULL` — we know we checked)
- Clear `search_synced_at`

*Phase 2 adds:*
- Delete transcript segment documents from Quickwit index (same `asset_id`-scoped delete-by-query)

**`GET /v1/assets/repair-summary`** — Add to `RepairSummary`:
- `missing_transcription: int` — count of videos where `has_transcript IS NULL`

**`GET /v1/assets/page`** — Add filter parameter:
- `missing_transcription: bool` — filter to assets matching the new condition

#### Search endpoint changes

**`GET /v1/search`** — Add `"transcript"` hit type:

```python
class SearchHit(BaseModel):
    type: Literal["image", "scene", "transcript"]

    # ... existing fields ...

    # Transcript-only fields
    start_ms: int | None = None    # already exists (shared with scene)
    end_ms: int | None = None      # already exists (shared with scene)
    snippet: str | None = None     # NEW: matching SRT text (few segments around match)
    language: str | None = None    # NEW: transcript language
```

The `snippet` field contains the matched SRT segment text plus adjacent segments for context. This gives the client enough text to display a meaningful preview. The `start_ms`/`end_ms` cover the full snippet range (min start of included segments, max end of included segments).

**Search flow for transcripts:**
1. Query Quickwit transcript index (BM25 on `text` field), request more hits than `limit` to allow for deduplication (e.g., `limit * 3`)
2. Group hits by `asset_id`, keep only the best-scoring segment per asset
3. For the winning segment, expand context: fetch the segment immediately before and after (by `start_ms` ordering within the same asset) from the Quickwit results or via a follow-up query
4. Build snippet: concatenate text of [prev, match, next] segments. Set `start_ms` = min of included, `end_ms` = max of included.
5. Look up asset metadata (library_name, thumbnail_key, etc.) via a batch asset fetch
6. Return as `SearchHit(type="transcript", ...)`

**Deduplication policy:** One hit per video. If a video has matches at 00:05:00 and 01:30:00, only the higher-scoring match is returned. This keeps the result list scannable. To see all matches within a video, the user opens the Lightbox and reads the full transcript (or searches within the TranscriptViewer — a potential future enhancement).

### CLI Changes

**New enrich job type: `transcribe`**

Added to `ENRICH_TYPES` and `REPAIR_TYPES`:
```python
ENRICH_TYPES = ("embed", "vision", "faces", "redetect-faces", "ocr", "transcribe", "video-scenes", "scene-vision", "search-sync", "all")
```

**New CLI config fields:**
```python
whisper_model: str = "small"            # tiny, base, small, medium, large-v3
transcribe_concurrency: int = 1         # sequential by default (GPU-bound)
```

**Transcription flow in `run_repair()`:**
```
1. Page missing: _page_missing(client, library_id, missing_transcription=True)
2. For each video asset:
   a. Extract audio: ffmpeg -i <source> -vn -ar 16000 -ac 1 -f wav <temp.wav>
   b. Transcribe: subprocess → faster-whisper → SRT string
   c. If no speech detected: POST empty transcript (sets has_transcript=false)
   d. If speech detected: POST SRT to /v1/assets/{id}/transcript
3. Batch not applicable (each transcription calls the existing single-asset endpoint)
```

**Source file access:** Unlike OCR (which reads from the proxy cache), transcription needs the audio track from the *source video file*. This means transcription can only run on the machine that has the source files — same constraint as scan. The proxy cache does not contain audio.

**Concurrency:** Default 1 (sequential). Whisper saturates one GPU. CPU-mode is slow enough that parallelism doesn't help much (memory-bound). User can increase if they have multiple GPUs or want to overlap ffmpeg extraction with inference.

### UI Changes

**Lightbox transcript section — add Download button:**

When a transcript exists, show three actions: `Download` | `Replace` | `Remove`

Download creates a client-side blob from `detail.transcript_srt` and triggers a download as `{rel_path_stem}.srt`.

**Search results — transcript hit rendering:**

A transcript search hit renders differently from image and scene hits:
- No thumbnail (server doesn't have one; the client generates preview clips locally)
- Show video icon + rel_path
- Show `snippet` text with the matching portion highlighted
- Show timestamp range: `[01:23:45 – 01:24:02]`
- Click navigates to the video asset in the Lightbox, scrolled to the relevant transcript segment

### Search Sync

**Phase 1:** The asset-level `transcript_text` field is already synced to the asset Quickwit index by the existing search sync sweep. Submitting a transcript clears `search_synced_at`, and the sweep re-indexes the asset document (which includes `transcript_text`). No new sweep logic needed — basic transcript search ("find videos that mention X") works via the asset index.

**Phase 2 adds per-segment sync:** The `run_search_sync_sweep()` function adds a third pass:
1. Asset sync (existing)
2. Scene sync (existing)
3. Transcript segment sync — for assets where `transcribed_at > search_synced_at`, parse SRT into segments, delete old documents from the transcript index (scoped by tenant index + `asset_id`), ingest new segment documents. This enables timestamp-level search results.

### Dependency

**New package: `faster-whisper`**

Added to the `workers` extra in `pyproject.toml` (client-side only, not installed on the server):
```toml
workers = [
    # ... existing deps ...
    "faster-whisper>=1.0.0",
]
```

`faster-whisper` pulls in `ctranslate2` (the inference backend) and `tokenizers`. No PyTorch dependency — it uses CTranslate2's own runtime. This is lighter than the original Whisper which requires torch.

Model weights are downloaded on first use to `~/.cache/huggingface/` (CTranslate2 converted models). The `small` model is ~460MB.

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| Video with no speech (birds, music) | VAD filters out non-speech. `_transcribe_one` returns empty SRT. Server sets `has_transcript = false`. Enrich skips on next run. |
| Video with background music + speech | VAD isolates speech segments. Whisper transcribes speech portions. Music-only segments are gaps in the SRT. |
| Very long video (3+ hours) | Audio extraction is fast (seconds). Whisper processes sequentially — ~2 min GPU, ~15 min CPU for 3 hours. Memory is bounded (CTranslate2 streams). Single temp WAV file (~170MB) cleaned up after. |
| Very short video (< 5 seconds) | Whisper may not have enough audio for reliable language detection. Transcription still runs; results may be lower quality. |
| Video with no audio track | ffmpeg audio extraction fails (exit code != 0, or ffprobe reports no audio stream). Deterministic — retry won't help. Asset marked `has_transcript = false`. |
| Transient failure (OOM, disk full, subprocess crash) | `_transcribe_one()` returns `None`. `has_transcript` stays `NULL`. Asset will be retried on next enrich run. |
| Non-English speech | Whisper auto-detects language. Stored in `transcript_language`. Search works across languages (Quickwit tokenizer handles Unicode). |
| Multi-language video | Whisper commits per-segment. Results are mixed but usable. `transcript_language` reflects the dominant language. |
| User uploads SRT over auto-generated | Existing behavior: `POST /v1/assets/{id}/transcript` replaces the old SRT. `has_transcript` stays `true`. Phase 2: Quickwit segments are re-indexed. |
| User deletes transcript | `DELETE /v1/assets/{id}/transcript` clears SRT, sets `has_transcript = false`. Phase 2: Quickwit segments deleted. Enrich will NOT re-transcribe (flag is `false`, not `NULL`). |
| User wants to re-transcribe after delete | Run `enrich --job-type transcribe --force`. Force mode resets `has_transcript` to `NULL` for the library, then re-runs. |
| Transcript search returns multiple segments from same video | Deduplication: return best-scoring segment per asset with adjacent context. One hit per video, not N. |
| Source files not available (enrich on different machine) | Transcription skipped — needs source video for audio extraction. Unlike OCR/vision (proxy cache), audio is not cached. Log warning and skip. |
| GPU not available | CTranslate2 falls back to CPU. Slower but correct. Log the device at startup. |
| Memory accumulation across batch | CTranslate2 does not have the ONNX Runtime leak, but subprocess isolation still ensures bounded memory and clean GPU contexts per job. Model weights (~460MB for `small`) are loaded fresh per subprocess and freed on exit. |
| Quickwit transcript index doesn't exist | Created on first ingest (same pattern as scene index: `ensure_tenant_transcript_index`). |
| SRT with HTML tags or formatting | `parse_srt_to_text` already strips to plain text. Segment parser should also strip tags. |
| Concurrent transcription + manual upload | Last writer wins. Manual upload replaces auto-generated. Both go through the same endpoint. |
| `enrich --job-type all` | Transcription runs as part of `all`, after OCR and before `search-sync`. Order: embed → vision → faces → ocr → transcribe → video-scenes → scene-vision → search-sync. |

## Code References

| Area | File | Notes |
|------|------|-------|
| Transcript API endpoints | `src/api/routers/assets.py:986-1061` | Existing POST/DELETE, needs `has_transcript` + Quickwit sync |
| SRT utilities | `src/core/srt.py` | Add `parse_srt_segments()` for structured parsing |
| Search endpoint | `src/api/routers/search.py` | Add `"transcript"` hit type |
| SearchHit model | `src/api/routers/search.py` | Add `snippet`, `language` fields |
| Quickwit client | `src/search/quickwit_client.py` | Add transcript index methods (pattern: scene index) |
| Search sync sweep | `src/search/sync.py` | Add transcript segment sync pass |
| Asset model | `src/models/tenant.py:145-150` | Existing transcript columns, add `has_transcript` |
| Repair types | `src/cli/repair.py:39-40` | Add `"transcribe"` |
| Enrich types | `src/cli/main.py:869` | Add `"transcribe"` |
| CLI config | `src/cli/config.py` | Add `whisper_model`, `transcribe_concurrency` |
| Missing conditions | `src/repository/tenant.py:45-64` | Add `missing_transcription` |
| Repair summary | `src/api/routers/assets.py:340-350` | Add `missing_transcription` count |
| Page endpoint | `src/api/routers/assets.py:180-270` | Add `missing_transcription` filter |
| Quickwit scene schema | `quickwit/scene_index_schema.json` | Template for transcript schema |
| TranscriptViewer | `src/ui/web/src/components/TranscriptViewer.tsx` | Existing, no changes needed |
| Lightbox transcript | `src/ui/web/src/components/Lightbox.tsx:963-1030` | Add download button |
| Search results UI | `src/ui/web/src/pages/SearchPage.tsx` (or equivalent) | Render transcript hits |

## Doc References

- `docs/cursor-api.md` — New transcript search hit type, updated repair-summary
- `docs/cursor-cli.md` — New `transcribe` enrich job type, whisper config
- `docs/architecture.md` — Add transcription to enrichment pipeline diagram

## Build Phases

### Requirements

Every phase must satisfy all of the following before it is marked complete:

1. **Tests**: New backend tests for every endpoint and repository method. Edge cases from the table above must be covered as they become relevant. **All tests must pass** — not just new or affected tests, the entire suite (`uv run pytest tests/`). No phase is done until the full suite is clean.
2. **Types**: Frontend TypeScript must compile cleanly (`npx tsc --noEmit`) — required only when the phase changes API contracts, shared types, or frontend code.
3. **Build**: Vite must build without errors (`npx vite build`) — required when frontend is affected.
4. **Documentation**: Relevant docs updated to reflect changes in the phase.
5. **Progress**: The phase status table above is updated when a phase completes.
6. **Forward compatibility**: Implementation must read ahead to future phases and ensure data model, API shapes, and component interfaces are set up correctly.
7. **Backward compatibility**: If current implementation invalidates or changes assumptions in a previous or future phase, those phases must be updated in this document before the current phase is marked complete.

### Phase 1 — Whisper Engine + Enrich Job Type

**Deliverables:**
- `faster-whisper` added to `workers` extra in `pyproject.toml`
- `_transcribe_one()` function: ffmpeg audio extraction → subprocess Whisper → SRT string (or empty on no speech)
- `has_transcript` column on `assets` table (migration, backfill from existing data, drop/recreate `active_assets` view)
- `missing_transcription` in `MISSING_CONDITIONS`
- `missing_transcription` count in `RepairSummary` and filter in page endpoint
- `transcribe` added to `ENRICH_TYPES` and `REPAIR_TYPES`
- Transcription section in `run_repair()`: page missing → extract audio → Whisper → POST to existing transcript endpoint
- `POST /v1/assets/{id}/transcript` sets `has_transcript` (true/false)
- `DELETE /v1/assets/{id}/transcript` sets `has_transcript = false`
- `whisper_model` and `transcribe_concurrency` in `CLIConfig`
- `--force` support: resets `has_transcript` to `NULL` for the library
- Tests: transcribe with speech, transcribe with silence (VAD), no audio track, has_transcript flag behavior, missing condition SQL, repair summary count

**Does NOT include:** Transcript search hit type, Quickwit transcript index, per-segment indexing, UI changes. Phase 1 search works via the existing asset-level `transcript_text` field in the asset Quickwit index — sufficient for "find videos that mention X" queries.

**Read-ahead:** Phase 2 needs `parse_srt_segments()` for per-segment Quickwit indexing. Phase 1 should add this to `src/core/srt.py` with tests, even though Phase 1 doesn't use it — having it tested early de-risks Phase 2.

**Done when:**
- [ ] `lumiverb enrich --job-type transcribe` transcribes videos with speech and skips silent ones
- [ ] `has_transcript` flag prevents re-transcription on subsequent enrich runs
- [ ] All deliverables implemented
- [ ] Tests written and passing (`uv run pytest tests/`)
- [ ] Docs updated (cursor-cli.md: transcribe job type)
- [ ] Phase status updated above

### Phase 2 — Transcript Search

**Deliverables:**
- `quickwit/transcript_index_schema.json` — per-segment index schema
- `build_transcript_document()` in `src/search/quickwit_client.py` — per-segment document builder, IDs as `{asset_id}_{start_ms}_{end_ms}`
- Quickwit client methods: `ensure_tenant_transcript_index`, `ingest_tenant_transcript_documents`, `search_tenant_transcripts`, `delete_tenant_transcript_documents` (delete-by-query scoped to `asset_id` within tenant index)
- `POST /v1/assets/{id}/transcript` — on submit, parse SRT via `parse_srt_segments()`, delete old segment documents (by `asset_id`), ingest new segment documents
- `DELETE /v1/assets/{id}/transcript` — delete segment documents from Quickwit
- `GET /v1/search` — new `"transcript"` hit type with `snippet`, `start_ms`, `end_ms`, `language`
- Per-asset deduplication: best-scoring segment per video, expand with 1 adjacent segment each side, merged `start_ms`/`end_ms` = min/max of included segments
- Search sync sweep: third pass for transcript segment re-indexing (assets where `transcribed_at > search_synced_at`)
- Tests: transcript segment indexing, search returns transcript hits, deduplication, snippet context window, delete removes segments, segment ID stability

**Does NOT include:** UI changes, download button, search result rendering.

**Read-ahead:** Phase 3 renders transcript hits differently from image/scene hits. Phase 2 must ensure the response shape has enough information for the UI (snippet text, timestamp range, asset metadata).

**Done when:**
- [ ] Searching for spoken text returns `"transcript"` hits with correct timestamps
- [ ] Transcript upload/delete correctly syncs Quickwit segments
- [ ] Deduplication returns one hit per video
- [ ] All deliverables implemented
- [ ] Tests written and passing (`uv run pytest tests/`)
- [ ] Docs updated (cursor-api.md: transcript search hit type)
- [ ] Phase status updated above

### Phase 3 — UI: Download, Upload, Search Results

**Deliverables:**
- Lightbox: Download SRT button (blob URL, filename `{stem}.srt`)
- Lightbox: Upload-over-existing flow (replace button always visible when transcript exists)
- Search results: Render `"transcript"` hits with video icon, snippet text, timestamp range, rel_path
- Search results: Click navigates to video in Lightbox
- TypeScript types updated for new `SearchHit` fields (`snippet`, `language`, `type: "transcript"`)

**Does NOT include:** Inline SRT editing (deferred to native client), video playback at timestamp.

**Done when:**
- [ ] SRT download works from Lightbox
- [ ] Upload-over-existing replaces transcript and re-indexes
- [ ] Transcript search hits render correctly in search results
- [ ] All deliverables implemented
- [ ] TypeScript compiles cleanly (`npx tsc --noEmit`)
- [ ] Vite builds without errors (`npx vite build`)
- [ ] Docs updated
- [ ] Phase status updated above

## Alternatives Considered

**OpenAI Whisper (original PyTorch implementation).** Same model weights but 4x slower and requires PyTorch. Since `faster-whisper` uses CTranslate2 (a lighter C++ runtime), it avoids adding torch as a dependency for transcription. The existing CLIP/InsightFace pipeline already has torch, but keeping transcription independent of it is cleaner for future dependency management.

**Server-side transcription.** Run Whisper on the API server instead of the client. Rejected because: (1) the VPS has no GPU, (2) it would require uploading source video files to the server (violates privacy-first principle), (3) it adds operational complexity (GPU instances, job queues). Client-side transcription matches the existing architecture where all inference runs locally.

**whisper.cpp (C++ implementation).** Faster than PyTorch Whisper but harder to integrate with Python. `faster-whisper` via CTranslate2 gets most of the speed benefit while staying in the Python ecosystem. whisper.cpp would be the right choice for the native macOS client.

**Store segments in a database table instead of Quickwit.** A `transcript_segments` table with `asset_id`, `start_ms`, `end_ms`, `text` would work for timestamp lookup but adds a table that grows with content (unlike the current pattern where Quickwit holds search data and Postgres holds metadata). Quickwit is already the search engine — putting segments there keeps the architecture consistent.

**Index full transcript text in the asset index instead of per-segment.** The asset index already has `transcript_text` for basic search. But matching "budget meeting" in a 3-hour transcript doesn't tell you *when* it was said. Per-segment indexing is needed for timestamp-level results. Both indexes are useful: the asset index finds "videos that mention X", the transcript index finds "the moment in the video where X was said."

## What This Does NOT Include

- **Inline SRT editing in the web UI** — Complex UI pattern (contenteditable with timestamp sync). Better suited for a native client with proper text editing controls. The download → edit → upload workflow covers this.
- **Video playback at timestamp** — The web UI doesn't have a video player that can seek to a timestamp. Clicking a transcript search result opens the Lightbox (which shows the transcript viewer), not a video player. A native client would handle this natively.
- **Speaker diarization** — Identifying *who* is speaking (Speaker 1, Speaker 2). Whisper doesn't do this natively. Requires a separate model (pyannote.audio). Valuable but out of scope — could be a future enhancement.
- **Real-time transcription** — Live transcription during video playback. Out of scope — this is a batch processing pipeline.
- **Translation** — Whisper can translate non-English speech to English. Useful but adds complexity (which version to store, both?). Deferred.
- **Audio-only file support** — Lumiverb currently handles images and videos. Podcast/audio file support would require a new media type. Out of scope.
- **Subtitle rendering on video** — Burning SRT into video frames or overlaying during playback. Client-side concern, not server.
