# ADR-011: Ingest Refactor — Scan and Enrich

## Status

Proposed

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Scan command: discover, hash, EXIF, proxy, upload, cache | Not started |
| 2 | Enrich command: CLIP, vision, OCR, faces from cached proxies | Not started |
| 3 | Ingest convergence: `ingest` = scan + enrich, repair = enrich missing | Not started |
| 4 | Remove legacy ingest path | Not started |

## Overview

The current ingest pipeline (`lumiverb ingest`) is a monolithic per-image function that touches source files, generates proxies, calls vision APIs, computes CLIP embeddings, and submits faces — all in one pass. This creates several problems: source files must be accessible throughout the entire pipeline, proxy generation is repeated across ingest and repair, and the pipeline cannot be interrupted and resumed at a meaningful boundary.

This ADR splits ingest into two orthogonal operations: **scan** (the only operation that touches source files) and **enrich** (everything derived from the proxy). The 2048px proxy becomes the canonical artifact — scan produces it, enrich consumes it. The persistent proxy cache bridges the two phases, eliminating redundant I/O and enabling the architecture to port cleanly to a native macOS app.

## Motivation

- **Redundant proxy generation**: Ingest generates a 2048px proxy and uploads it. Repair/redetect-faces regenerates proxies from source files (reading 60MB RAW files) because the proxy cache is ephemeral. With 17K images, this is hours of wasted I/O on re-runs.
- **Source file coupling**: The entire pipeline requires source file access. CLIP, vision, OCR, and face detection only need the proxy, but they currently run in the same pass that reads source files. This prevents running enrichment on a different machine or after source files are offline.
- **No clean resume boundary**: If ingest crashes at image 5,000 of 17,000, there's no way to resume only the enrichment work. Repair can backfill missing stages, but it re-discovers everything from scratch.
- **Native app preparation**: A macOS app needs scan (filesystem watcher + proxy generation) and enrich (GPU inference) as independent operations. Getting the separation right in Python first de-risks the native port.
- **Ingest and repair are converging**: Repair already does enrichment — it pages missing assets, downloads proxies, runs inference, and submits results. The only thing ingest does that repair doesn't is scan. Making this explicit eliminates code duplication.

## Design

### Architecture

```
Source Files ──► SCAN ──► Server + Proxy Cache ──► ENRICH ──► Server
                  │                                    │
                  │  Touches source files               │  Only reads proxy cache
                  │  Produces 2048px proxy              │  Resizes as needed (1280px, etc.)
                  │  Extracts EXIF + SHA                │  Runs CLIP, vision, OCR, faces
                  │  Uploads to server                  │  Submits results to server
                  │  Caches proxy locally               │
                  ▼                                    ▼
              asset_id                           enriched metadata
```

The proxy cache (`~/.cache/lumiverb/proxies/`) is the handoff point. Scan writes to it; enrich reads from it. The cache is keyed by `asset_id` with a `.sha` sidecar for staleness detection.

### Scan

Scan is the only operation that touches source files. It produces three outputs per file: a server-side asset record, a server-side 2048px proxy, and a local proxy cache entry.

**Steps per file:**
1. Compute SHA-256 of source file
2. Extract EXIF metadata
3. Generate 2048px JPEG proxy from source (RAW extraction, resize)
4. Upload proxy + EXIF + SHA to server via `POST /v1/ingest` -> receive `asset_id`
5. Save proxy to local cache as `{asset_id}` with `{asset_id}.sha` sidecar

**Change detection:**
Before processing files, scan compares local filesystem against server state:

| State | Local file | Server asset | SHA match | Action |
|-------|-----------|-------------|-----------|--------|
| New | Exists | No match by rel_path | — | Full scan (SHA, EXIF, proxy, upload, cache) |
| Changed | Exists | Match by rel_path | No | Re-scan: update existing asset row (new SHA, new proxy, re-upload). `asset_id` is stable — the same asset record is updated in place via the existing upsert behavior of `POST /v1/ingest`. Enrichment flags (`missing_vision`, `missing_faces`, etc.) are reset server-side so enrich re-processes the asset with the new proxy. The proxy cache entry is overwritten with the new proxy bytes + updated `.sha` sidecar. |
| Unchanged | Exists | Match by rel_path | Yes | Skip (verify proxy cache, populate if missing) |
| Deleted | Missing | Match by rel_path | — | Mark deleted on server |

For "unchanged" files where the proxy cache is missing (e.g., new machine, cleared cache), scan downloads the existing proxy from the server and caches it locally. This avoids re-reading the source file.

**Deletion detection and safety:**

Deletion detection requires a full-library scan — scan must walk the entire library (or `--path-prefix` subtree) to know which server-side assets no longer have local files. This means:

- **Full scan is required for deletion.** Incremental scan (process only new/changed) cannot detect deletions because it doesn't know what's missing. Scan always walks the full tree but skips unchanged files quickly (stat + SHA compare, no I/O for the file body).
- **Soft delete, not hard delete.** Detected deletions call the existing `DELETE /v1/assets` endpoint which sets `deleted_at` (soft delete / trash). The user can recover from the trash. This matches current ingest behavior.
- **Volume unmount protection.** If the library root is not accessible (e.g., external drive unmounted), scan refuses to run and prints an error. It does not treat an empty/missing root as "all files deleted." This check exists today in `run_ingest()` and carries forward.
- **`--path-prefix` scoping.** When `--path-prefix` is used, deletion detection is scoped to that subtree only. Assets outside the prefix are untouched. This prevents a partial scan from trashing the whole library.

**What scan does NOT do:**
- CLIP embeddings
- Vision AI (describe, tags)
- OCR text extraction
- Face detection
- Search index sync

### Enrich

Enrich operates exclusively on proxy cache contents. It never touches source files. It is functionally identical to what `repair` does today, but generalized as the standard enrichment path.

**Steps per asset (as needed):**
1. Read proxy from local cache (resize to 1280px for consumers that need it)
2. Generate CLIP embedding -> `POST /v1/assets/{id}/embeddings`
3. Run vision AI (describe + tags) -> submit via ingest or metadata endpoint
4. Run OCR text extraction -> `POST /v1/assets/{id}/ocr`
5. Run face detection -> `POST /v1/assets/batch-faces`
6. Sync to search index

Each step is independent and idempotent. The server tracks which steps are complete per asset (`missing_embeddings`, `missing_vision`, `missing_faces`, `missing_ocr`). Enrich only processes what's missing.

**Proxy cache interaction:**
- If proxy is in cache with matching SHA -> use it
- If proxy is in cache with stale SHA -> download fresh from server, update cache
- If proxy is not in cache -> download from server, cache it
- Enrich never generates proxies from source files

### CLI Commands

```
lumiverb scan --library <name> [--path-prefix <subdir>] [--force]
```

Discover files, compute SHA, extract EXIF, generate 2048px proxy, upload to server, cache locally. `--force` re-scans unchanged files (re-generates proxy even if SHA matches).

```
lumiverb enrich --library <name> [--job-type <type>] [--concurrency N]
```

Run enrichment on assets with missing pipeline outputs. Same job types as current repair: `embed`, `vision`, `faces`, `ocr`, `search-sync`, `all`.

```
lumiverb ingest --library <name> [--concurrency N]
```

Sugar for `scan` then `enrich`. Preserves existing UX for users who want one command.

```
lumiverb repair --library <name> [--job-type <type>]
```

Becomes an alias for `enrich`. Kept for backward compatibility.

### API Changes

**No new endpoints required.** Scan uses the existing `POST /v1/ingest` endpoint. Enrich uses existing submission endpoints (`batch-faces`, `batch-ocr`, embeddings, etc.).

**One change to `POST /v1/ingest`:** Accept an optional `sha256` field in the form data. Scan computes SHA-256 of the **source file** and passes it through. The server stores it on the asset record. This hash represents source file identity, not proxy bytes — it's used for change detection across scan runs.

**SHA semantics and migration:**
- **Current behavior:** `sha256` is computed client-side from the source file during EXIF extraction and sent as part of the EXIF payload. The server stores it on the asset record but does not independently verify it against the uploaded proxy bytes (the proxy is a lossy derivative, so the hashes would never match).
- **New behavior:** `sha256` moves from the EXIF payload to a top-level form field, making the semantic explicit: this is the source file hash. No server-side validation change — the server still trusts the client-provided value.
- **Existing assets:** Assets ingested under the current pipeline already have correct source-file SHA-256 values. No migration needed — the stored values are already what change detection expects.
- **Change detection for old assets:** Works correctly. The stored `sha256` was always the source file hash. Scan compares local `compute_sha256(source)` against the server's stored value — same computation, same semantics.

### Proxy Cache

The persistent proxy cache (`~/.cache/lumiverb/proxies/`) becomes the central handoff between scan and enrich:

```
~/.cache/lumiverb/proxies/
  {asset_id}          # 2048px JPEG proxy (same as server proxy, pre-WebP conversion)
  {asset_id}.sha      # SHA-256 of source file (for staleness detection)
```

**Thread safety:** Multiple proxy gen threads write to different files simultaneously. Safe on POSIX (file writes to different paths are independent). For same-asset-id writes (e.g., user runs scan while enrich is reading), proxy writes should use atomic write-to-temp + rename to prevent enrich from reading a partial file. The `.sha` sidecar is written after the proxy, so a reader that sees a `.sha` file can trust the proxy is complete.

**Disk budget:** 17K images at ~100KB average = ~1.7GB. RAW-heavy libraries with large embedded JPEGs may skew higher. Acceptable for a local cache. A future `lumiverb maintenance cache-cleanup` command can prune entries for deleted assets.

**Cross-process sharing:** The subprocess face detection workers read from this cache via the path. No IPC needed — just filesystem.

### Data Flow Comparison

**Current (monolithic ingest):**
```
Source file → SHA → EXIF → proxy gen → vision AI → CLIP → upload(proxy+EXIF+vision+CLIP) → faces (separate)
```

All steps in one function. Source file held open (conceptually) throughout. If vision API is slow, proxy bytes sit in memory waiting.

**Proposed (scan + enrich):**
```
Scan:   Source file → SHA → EXIF → proxy gen → upload(proxy+EXIF+SHA) → cache proxy
Enrich: Cache → resize → [CLIP | vision | OCR | faces] → submit results
```

Clean separation. Scan is I/O-bound (disk read + network upload). Enrich is compute-bound (GPU inference + API calls). Different concurrency profiles, can run on different machines.

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| Source file changes between scan runs | Change detection compares SHA. Changed file gets re-scanned (new proxy, updated SHA). Enrich re-runs automatically because vision/faces/etc. are reset on re-upload. |
| Proxy cache cleared between scan and enrich | Enrich downloads proxy from server on cache miss. Slower but correct. |
| Scan interrupted mid-run | Assets already uploaded are durable on server. Re-running scan skips unchanged files via change detection. No partial state. |
| Enrich interrupted mid-run | Each enrichment step is independently tracked. Re-running enrich picks up where it left off via `missing_*` filters. |
| Asset deleted on server between scan and enrich | Enrich skips missing assets (404 from server). |
| Same library scanned from two machines | Both machines upload to same server. Change detection is server-authoritative. Last writer wins for changed files. Proxy caches are local to each machine. |
| RAW file with no embedded JPEG | Scan falls back to rawpy demosaicing (slow but correct). Proxy is generated at 2048px regardless of extraction method. |
| Video files | Scan handles video poster frame extraction (existing behavior). Video scene detection and enrichment remain in enrich phase. No change to video pipeline structure. |
| `--force` flag on scan | Re-generates proxy and re-uploads even for unchanged files. Useful after changing proxy generation logic. |
| Unchanged file, missing proxy cache | Scan downloads existing proxy from server and caches it. Does not re-read source file. |
| Library root unmounted or missing | Scan refuses to run with a clear error message. Does not treat missing root as "all files deleted." |
| `--path-prefix` used with deletion detection | Deletion is scoped to the prefix subtree only. Assets outside the prefix are untouched. |
| Scan and enrich run concurrently on same library | Safe. Scan writes proxies atomically (temp + rename). Enrich reads completed proxies. Enrich may skip an asset whose proxy is mid-write — it will pick it up on the next run. |
| Changed file resets enrichment | Re-uploading via `POST /v1/ingest` updates the proxy and resets enrichment flags server-side. Enrich automatically re-processes the asset. asset_id is stable. |

## Code References

| Area | File | Notes |
|------|------|-------|
| Current ingest entry | `src/cli/ingest.py:772` | `run_ingest()` — will be split into scan + enrich |
| Per-image processing | `src/cli/ingest.py:532` | `_process_and_ingest_one()` — monolithic, to be decomposed |
| Proxy generation | `src/cli/proxy_gen.py:23` | `generate_proxy_bytes()` — stays in scan, removed from enrich |
| Proxy cache | `src/cli/proxy_cache.py` | Already persistent, needs minor API adjustments |
| Face detection pipeline | `src/cli/repair.py:475` | `_run_face_pipeline()` — becomes part of enrich |
| Vision backfill | `src/cli/ingest.py:631` | `run_backfill_vision()` — becomes part of enrich |
| File discovery | `src/cli/ingest.py:667` | `_walk_library()` — moves to scan |
| Change detection | `src/cli/ingest.py:751` | `_fetch_existing_assets()` — enhanced for SHA comparison |
| Server ingest endpoint | `src/api/routers/ingest.py:267` | `create_and_ingest()` — minor change (accept sha256 input) |
| Repair command | `src/cli/repair.py:618` | `run_repair()` — converges with enrich |

## Doc References

- `docs/cursor-api.md` — Update ingest endpoint docs (sha256 field)
- `docs/cursor-cli.md` — Add scan/enrich commands, update ingest docs
- `docs/architecture.md` — Update processing pipeline diagram

## Build Phases

### Requirements

Every phase must satisfy all of the following before it is marked complete:

1. **Tests**: New backend tests for every endpoint and repository method. Edge cases from the table above must be covered as they become relevant. **All tests must pass** — not just new or affected tests, the entire suite (`uv run pytest tests/`). No phase is done until the full suite is clean.
2. **Types**: Frontend TypeScript must compile cleanly (`npx tsc --noEmit`) — required only when the phase changes API contracts, shared types, or frontend code. Phases that are purely Python/CLI (Phases 1, 2, 4) may skip this gate.
3. **Build**: Vite must build without errors (`npx vite build`) — same gate as types: required when frontend is affected.
4. **Documentation**: Relevant docs updated to reflect changes in the phase.
5. **Progress**: The phase status table above is updated when a phase completes.
6. **Forward compatibility**: Implementation must read ahead to future phases and ensure data model, API shapes, and component interfaces are set up correctly. If current work reveals changes needed in a future phase, update that phase's description.
7. **Backward compatibility**: If current implementation invalidates or changes assumptions in a previous or future phase, those phases must be updated in this document before the current phase is marked complete.

### Phase 1 — Scan Command

**Deliverables:**
- `lumiverb scan` CLI command: discover files, compute SHA, extract EXIF, generate 2048px proxy, upload via `POST /v1/ingest`, cache proxy locally
- Change detection: page existing assets, compare SHA, skip unchanged, re-scan changed, mark deleted
- Populate proxy cache for unchanged files (download from server if not cached)
- Progress bar with discovered/new/changed/unchanged/deleted counters
- `--force` flag to re-scan unchanged files
- `--path-prefix` filter (existing behavior from ingest)
- Tests: scan with new files, changed files, unchanged files, deleted files, interrupted scan resume

**Does NOT include:** CLIP, vision, OCR, face detection, search sync. These move to Phase 2.

**Read-ahead:** Phase 2 expects every scanned asset to have a proxy in the local cache. Scan must guarantee this — either by generating from source or downloading from server.

**Done when:**
- [ ] All deliverables implemented
- [ ] Tests written and passing (`uv run pytest tests/`)
- [ ] Docs updated (cursor-cli.md: scan command)
- [ ] Phase status updated above

### Phase 2 — Enrich Command

**Deliverables:**
- `lumiverb enrich` CLI command: read proxy from cache, run CLIP/vision/OCR/faces, submit results
- Enrich never reads source files. The proxy cache is the primary image source (populated by scan). On cache miss, enrich downloads the server proxy to populate the cache — slower but correct. This covers cases where the cache was cleared or enrich runs on a different machine than scan.
- Same `--job-type` flags as current repair: `embed`, `vision`, `faces`, `ocr`, `search-sync`, `all`
- Pipelined proxy resize + inference (existing `_run_face_pipeline` pattern, generalized)
- `--concurrency` flag
- Tests: enrich with full cache, enrich with cache miss (server fallback), enrich idempotency

**Does NOT include:** Change detection, proxy generation from source files, file discovery.

**Read-ahead:** Phase 3 wires `ingest` to call scan then enrich. Enrich must accept a "just scanned" asset list to avoid re-paging the entire library.

**Done when:**
- [ ] All deliverables implemented
- [ ] Tests written and passing (`uv run pytest tests/`)
- [ ] Docs updated (cursor-cli.md: enrich command)
- [ ] Phase status updated above

### Phase 3 — Ingest Convergence

**Deliverables:**
- `lumiverb ingest` becomes: call `scan` then call `enrich`. All code paths route through the Phase 1/2 implementations — `ingest` is a thin orchestrator, not a third implementation.
- `lumiverb repair` becomes an alias for `lumiverb enrich` with full option parity: `--job-type`, `--concurrency`, `--dry-run`, `--force`, `--library`. No flags are dropped.
- Scan passes list of newly scanned asset IDs to enrich to avoid redundant paging
- Single progress experience: scan phase shows file discovery, enrich phase shows enrichment progress
- The old `_process_and_ingest_one()` monolithic path is no longer called by any command. It remains in the codebase (dead code) for Phase 4 removal but is unreachable.
- Tests: `ingest` end-to-end does scan + enrich, `repair` still works with all existing flags, no test calls the old path

**Does NOT include:** Deleting dead code (Phase 4). Phase 3 makes the old path unreachable; Phase 4 removes it.

**Read-ahead:** Phase 4 is purely mechanical deletion. Phase 3 must verify that no tests, imports, or CLI commands reference the old path before Phase 4 runs `git rm`.

**Done when:**
- [ ] All deliverables implemented
- [ ] `ingest` and `repair` both route through scan/enrich
- [ ] Old monolithic path is unreachable (no callers)
- [ ] Tests written and passing (`uv run pytest tests/`)
- [ ] Docs updated (cursor-cli.md: updated ingest docs, repair alias note)
- [ ] Phase status updated above

### Phase 4 — Remove Legacy Ingest Path

**Deliverables:**
- Delete `_process_and_ingest_one()` and all per-image monolithic processing functions that are now dead code (verified unreachable in Phase 3)
- Delete `_face_batch_worker` from ingest.py (single implementation lives in shared module or repair.py)
- Delete inline vision/CLIP calls from the old ingest path
- Clean up orphaned imports
- Verify no external callers depend on removed functions (grep for function names across codebase)
- Tests: full suite passes, no regressions

**Does NOT include:** New features. This is purely deletion of code that Phase 3 made unreachable.

**Done when:**
- [ ] All dead code removed
- [ ] Tests written and passing (`uv run pytest tests/`)
- [ ] No references to deleted functions remain
- [ ] Phase status updated above

## Alternatives Considered

**Keep monolithic ingest, just add caching.** This is what we've been doing incrementally (persistent proxy cache, batch endpoints). It improves performance but doesn't address the fundamental coupling between source file access and enrichment. The native macOS app needs these separated regardless.

**Server-side enrichment queue.** Instead of client-side enrich, the server could queue enrichment jobs and process them. This would decouple the CLI entirely but adds operational complexity (job queue infrastructure, GPU on server). The current client-side model works well for a single-user product and keeps GPU processing local.

**Event-driven pipeline (scan emits events, enrich subscribes).** Cleaner separation but over-engineered for the current scale. The phased CLI approach achieves the same decoupling without infrastructure overhead. A native app could adopt an event-driven model internally.

**Skip the proxy cache, always download from server.** Simpler but slow — downloading 17K proxies over the network on every enrich run defeats the purpose. The cache makes enrich fast for the common case (same machine that scanned).

## What This Does NOT Include

- **Real-time filesystem watching** — Scan is currently batch/on-demand. A native macOS app would use FSEvents for continuous scanning. The scan/enrich separation enables this but doesn't implement it.
- **Multi-machine coordination** — Two machines can scan and enrich independently, but there's no job queue or lock to prevent duplicate enrichment work. Acceptable for single-user.
- **Proxy format migration** — The server stores proxies as WebP. The local cache stores JPEG (pre-WebP conversion). A future optimization could align these, but it's not necessary for correctness.
- **Video pipeline refactor** — Video ingest (poster frame, scene detection, rep frame extraction) stays in its current structure. The scan/enrich separation applies primarily to images. Video enrichment (scene vision) already follows the enrich pattern.
- **Cache eviction policy** — The proxy cache grows unbounded. A future `maintenance cache-cleanup` command can prune entries for deleted assets. Not critical at current scale (~1.7GB for 17K images).
