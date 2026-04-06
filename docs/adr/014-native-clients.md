# ADR-014: Native Clients (macOS / iOS)

## Status

Proposed

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Python restructure: clean client/server separation, test boundary audit | Not started |
| 1 | Swift project scaffold: Xcode project, shared package, API client, auth | Not started |
| 2 | macOS browse shell: library list, media grid, lightbox, search | Not started |
| 3 | macOS background scan: file watching, hashing, proxy generation, upload | Not started |
| 4 | macOS enrichment: CoreML CLIP, CoreML ArcFace, Apple Vision OCR, whisper.cpp | Not started |
| 5 | iOS browse app: shared UI from Phase 2 adapted for iOS | Not started |
| 6 | Face tagging UI (both platforms) | Not started |

## Overview

Lumiverb's CLI is a Python application that runs on the machine where source files live. It handles file discovery, SHA hashing, proxy generation, ML inference (CLIP, face detection, transcription, OCR), and communicates with the API server over REST. The web UI provides browse/search/tag capabilities.

This ADR introduces native macOS and iOS clients built with Swift/SwiftUI. The macOS app replaces both the CLI and web UI for daily use — a menu bar app that continuously watches libraries, processes new content, and provides a native browse/search/tag interface. The iOS app is browse-only (search, face tagging, basic operations — no ingestion or enrichment).

The Python CLI remains as a maintenance and power-user tool. It is not deprecated — it's the escape hatch for batch operations, scripting, and platforms where native clients don't exist.

Phase 0 restructures the Python codebase to establish clean client/server boundaries before any Swift code is written. This ensures the API contract is the only coupling point between clients and the server.

## Motivation

- **Installation friction**: The Python CLI requires `uv`, Python 3.12+, and heavy ML dependencies (PyTorch, ONNX Runtime, InsightFace, faster-whisper). A native app is a single `.app` bundle — drag to Applications, done.
- **Background processing**: The CLI must be invoked manually (`lumiverb scan`, `lumiverb enrich`). A menu bar app watches for changes continuously.
- **Native experience**: The web UI works but feels like a web app. A SwiftUI app gets native scrolling, keyboard shortcuts, drag-and-drop, system integration (Spotlight, Quick Look, Share Sheet).
- **iOS access**: No current way to browse your library from a phone/tablet. An iOS app provides search and face tagging on the go.
- **Multi-client future**: Clean client/server separation benefits all clients — Python CLI, macOS app, iOS app, and eventually Windows.

## Design

### Technology Choices

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Language | **Swift** | First-class Apple platform support, open source, no runtime dependency |
| UI framework | **SwiftUI** | 80-90% code sharing between macOS and iOS, declarative (React-like) |
| App model (macOS) | **Menu bar app** | Single binary, no dock icon, persistent background processing. User quits app = processing stops (intuitive). No XPC/LaunchAgent complexity. |
| Distribution | **Notarized .dmg** | Full file system access (no App Store sandbox restrictions). Homebrew cask is just a pointer to the .dmg — add later with zero app changes. |
| ML inference | **CoreML** (CLIP, ArcFace) + **whisper.cpp** (transcription) + **Apple Vision** (OCR, face detection) | Native acceleration (Neural Engine, GPU), no Python runtime |
| Video processing | **ffmpeg** (subprocess) | Same as today — stable external binary, not a runtime dependency |
| Image processing | **ImageIO / CGImage** | Built-in, supports JPEG/HEIC/RAW/WebP, hardware-accelerated resize |
| HTTP client | **URLSession** | Built-in, supports background transfers |

### Processing Pipeline: Python → Native Mapping

| Capability | Python (CLI today) | Swift (native app) | Notes |
|---|---|---|---|
| File discovery | `os.walk` + stat | `FSEvents` + `FileManager` | FSEvents provides real-time file change notifications — no polling needed |
| SHA-256 hashing | `hashlib` | `CryptoKit` | Built-in, hardware-accelerated on Apple Silicon |
| EXIF extraction | `pyexiftool` (subprocess) | `ImageIO` / `CGImageSource` | Built-in, reads all standard EXIF/IPTC/XMP. Falls back to exiftool subprocess for exotic formats if needed |
| Image proxy generation | `pyvips` (Lanczos resize) | `CGImageSourceCreateThumbnailAtPixelSize` | Decodes-and-resizes in one pass — never loads full resolution into memory. Bonus: native RAW support |
| Video poster frame | `ffmpeg` subprocess | `ffmpeg` subprocess | Same approach |
| Video preview clip | `ffmpeg` subprocess | `ffmpeg` subprocess | Same approach |
| Video scene segmentation | `ffmpeg` streaming + `imagehash` + OpenCV | `ffmpeg` streaming + `CIImage` perceptual hash + `Accelerate` Laplacian | Same algorithm, native image processing |
| CLIP embeddings | `open-clip-torch` (PyTorch) | **CoreML** — ViT-B/32 converted via `coremltools` | One-time conversion. Neural Engine acceleration. Minor float divergence from PyTorch (cosine sim >0.999) — validate with test set |
| Face detection | InsightFace RetinaFace (ONNX) | **Apple Vision** `VNDetectFaceRectanglesRequest` + `VNDetectFaceLandmarksRequest` | Built-in, no model files needed |
| Face embeddings | InsightFace ArcFace (ONNX) | **CoreML** — ArcFace converted via `coremltools` | Apple has NO public face embedding API. Must bring own model. Use MobileNet-based variant (no deformable convolutions). Existing 512-dim embeddings stay compatible. |
| Vision AI (captions/tags) | OpenAI-compatible API | OpenAI-compatible API (same) | No native equivalent for image captioning |
| OCR | OpenAI-compatible API | **Provider pattern**: Apple Vision `VNRecognizeTextRequest` (default, local) OR OpenAI-compatible API (configurable) | Apple Vision is excellent for printed text, fully offline. Config toggle for users who prefer the AI-based approach. |
| Transcription | `faster-whisper` (subprocess) | **whisper.cpp** — linked as C library or subprocess | Mature, CoreML acceleration available, Silero VAD for silence filtering. Tiny/base models work on iOS; small/medium for macOS. |
| REST API client | `httpx` | `URLSession` | Built-in |
| Path filtering | `fnmatch` glob matching | `fnmatch` (C stdlib) or `NSPredicate` | Reimplemented independently — shared specification, not shared code |

### Provider Pattern for OCR

The native app uses a provider pattern for capabilities where both local and API-based options exist:

```
Protocol: TextExtractionProvider
  ├── AppleVisionTextProvider (default — offline, free)
  └── OpenAICompatibleTextProvider (configurable — uses vision API)

Config: Settings > Processing > OCR Provider: [Local (Apple Vision) | API]
```

Default is local processing. Same pattern can extend to other capabilities if Apple adds native image captioning in the future.

### Project Structure

```
/src                          ← Python (API server + CLI — unchanged location)
/src/ui/web                   ← React web UI (unchanged)
/clients
    /lumiverb-app             ← Xcode project (one project, two targets)
        /Shared               ← Shared SwiftUI views, API client, models, providers
            /API              ← URLSession client, request/response types
            /Models           ← Swift equivalents of API types
            /Views            ← Shared SwiftUI views (media grid, lightbox, search)
            /Providers        ← ML provider protocols + implementations
        /macOS                ← macOS target (menu bar app, file watching, processing)
        /iOS                  ← iOS target (browse-only shell)
        /CoreML               ← Converted .mlmodel files (CLIP, ArcFace)
        /Resources            ← App icons, assets
```

Single Xcode project with two targets (macOS, iOS). Shared code lives in a Swift Package within the project. Each target has a thin platform-specific shell.

### macOS App Architecture

```
┌─────────────────────────────────────────────┐
│  Menu Bar Icon                               │
│  ┌─────────────────────────────────────────┐ │
│  │  Status: Watching 3 libraries           │ │
│  │  Last scan: 2 minutes ago               │ │
│  │  Processing: 4 of 127 new files         │ │
│  ├─────────────────────────────────────────┤ │
│  │  Open Lumiverb          ⌘O              │ │
│  │  Scan Now               ⌘S              │ │
│  │  Pause Processing       ⌘P              │ │
│  ├─────────────────────────────────────────┤ │
│  │  Settings...            ⌘,              │ │
│  │  Quit Lumiverb          ⌘Q              │ │
│  └─────────────────────────────────────────┘ │
└─────────────────────────────────────────────┘

Main window: SwiftUI NavigationSplitView
  ├── Sidebar: Library list
  ├── Content: Media grid (LazyVGrid + async image loading)
  └── Detail: Lightbox (proxy image + metadata + face tags)
```

**Background processing lifecycle:**
1. App launches at login (optional, configurable via Login Items)
2. `FSEvents` watches all configured library root paths
3. On file change: debounce 5 seconds, then run scan cycle (discover → hash → proxy → upload)
4. Enrichment runs after scan completes (CLIP → faces → OCR → transcription)
5. Processing is pausable from the menu bar
6. App quit = processing stops. Next launch resumes from where server state left off.

### iOS App Scope

iOS is **browse-only** — no file watching, no scanning, no enrichment. It talks to the API server over REST and provides:

- Library list
- Media grid with infinite scroll
- Search (text + similar-by-image using camera roll or existing asset)
- Lightbox with metadata
- Face tagging (Phase 6)
- Download proxy/thumbnail to camera roll

Face bounding boxes and embeddings come from the server (populated by macOS enrichment or the Python CLI). iOS never runs face detection or embedding — it only displays and assigns faces.

### API Contract

The native clients use the **exact same REST API** as the Python CLI and web UI. No new endpoints are needed for Phases 1-3. Phase 4 (enrichment) may benefit from a `/v1/similar-by-vector` endpoint to keep CLIP client-side, but this is optional — the existing `/search-by-image` endpoint works from day one.

The API contract is documented in `docs/cursor-api.md`. The Swift API client is generated from this documentation, not from shared Python code.

### CoreML Model Conversion

One-time conversion, checked into the repo as `.mlmodel` files:

| Model | Source | Conversion Path | Output Dimensions |
|-------|--------|-----------------|-------------------|
| CLIP ViT-B/32 (image encoder) | `open-clip-torch` PyTorch weights | PyTorch → ONNX → CoreML via `coremltools` | 512-dim float vector |
| CLIP ViT-B/32 (text encoder) | `open-clip-torch` PyTorch weights | PyTorch → ONNX → CoreML via `coremltools` | 512-dim float vector |
| ArcFace (face embedding) | InsightFace `buffalo_l` ONNX | ONNX → CoreML via `coremltools` | 512-dim float vector |

**Validation requirement:** Before shipping, run both Python and CoreML inference on a test set of 100 images. Verify cosine similarity > 0.999 between PyTorch and CoreML embeddings. Any divergence beyond this threshold indicates a conversion problem.

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| Library root on unmounted volume (NAS offline) | macOS app shows "Volume unavailable" status in menu bar. Skips that library until volume reappears. Does not delete assets. |
| App quit during active scan | Processing stops immediately. Server has partially uploaded batch. Next launch: scan picks up from server state (new files detected again, unchanged files skipped). No orphaned state. |
| ffmpeg not installed | App checks on first launch. Shows setup assistant: "Lumiverb requires ffmpeg for video processing. Install with: `brew install ffmpeg`" or provide download link. Image processing works without ffmpeg. |
| whisper.cpp not available | Transcription skipped. Other enrichment proceeds normally. Prompt user to install or download. |
| CoreML model missing | Enrichment that requires the model is skipped with a warning. App still functions for browse/search. |
| Existing Python CLI running alongside | No conflict. Both are API clients. Server handles concurrent requests. The scan mtime+size check prevents redundant work — whichever client processed a file first, the other sees it as unchanged. |
| iOS on cellular | Proxy images are ~200KB each. Thumbnails ~15KB. Search results load thumbnails first. Lightbox loads proxy on demand. No source file access. Respect iOS low-data mode. |
| CLIP embedding divergence (PyTorch vs CoreML) | Similarity search results may differ slightly in ordering. Acceptable — ranking differences are marginal (cosine sim >0.999). Do NOT re-embed existing assets unless divergence exceeds threshold. |
| Apple Vision OCR vs API OCR produce different text | Expected. The three-state `has_text` flag means switching OCR providers doesn't re-process already-checked assets. To re-OCR with a different provider, use `--force` on the CLI or a "Re-process" option in the native app. |

## Code References

### Phase 0 — Python restructure targets

| Area | Current Location | Issue |
|------|-----------------|-------|
| Server config + database | `src/core/config.py`, `src/core/database.py` | Server-only code in shared `core/` package |
| SRT parsing | `src/core/srt.py` | Only used by API (server-only), lives in `core/` |
| Quickwit purge | `src/workers/quickwit.py` | Only used by API, lives in client-side `workers/` |
| CLIP provider | `src/workers/embeddings/clip_provider.py` | Used by API for query-time embedding — should move server-side or API gets a vector-only endpoint |
| Asset status constants | `src/core/asset_status.py` | Used by both — truly shared, but trivial |
| Path filter | `src/core/path_filter.py` | Used by both — shared specification, small module |
| IO utils | `src/core/io_utils.py` | Used by both — truly shared, trivial |
| File extensions | `src/core/file_extensions.py` | Used by CLI only, lives in `core/` |
| Logging config | `src/core/logging_config.py` | Used by CLI only, lives in `core/` |

### Existing test gaps

| Area | File | Issue |
|------|------|-------|
| File extensions | `src/core/file_extensions.py` | No tests |
| IO utils | `src/core/io_utils.py` | No tests |
| SRT parsing | `tests/test_srt.py` | Tests exist but no pytest marker (should be `@pytest.mark.fast`) |
| 12 test files | Various | No pytest markers — should be classified as fast or slow |

## Doc References

- `docs/cursor-api.md` — API reference (authoritative contract for all clients)
- `docs/cursor-cli.md` — CLI reference (update to clarify CLI as maintenance tool)
- `docs/architecture.md` — System design (update with native client architecture)

## Build Phases

### Requirements

Every phase must satisfy all of the following before it is marked complete:

1. **Tests**: Edge cases from the table above must be covered as they become relevant. **All existing tests must continue to pass.** No phase is done until the relevant suite is clean.
2. **Python phases (Phase 0):** Full Python test suite passes (`uv run pytest tests/`). TypeScript compiles (`npx tsc --noEmit`). Vite builds (`npx vite build`). Deploy scripts verified.
3. **Swift phases (Phases 1-6):** Both Xcode targets build (`xcodebuild`). Swift tests pass. If the phase touches the Python server (new endpoints, schema changes), the Python test suite must also pass.
4. **Documentation**: Relevant docs updated to reflect changes in the phase.
5. **Progress**: The phase status table above is updated when a phase completes.
6. **Forward compatibility**: Implementation must read ahead to future phases and ensure data model, API shapes, and component interfaces are set up correctly. If current work reveals changes needed in a future phase, update that phase's description.
7. **Backward compatibility**: If current implementation invalidates or changes assumptions in a previous or future phase, those phases must be updated in this document before the current phase is marked complete.

### Phase 0 — Python Client/Server Separation

Restructure the Python codebase so that client-only, server-only, and shared code live in clearly separated packages. No Swift code in this phase — this is about establishing clean boundaries.

**Current structure:**
```
src/
├── api/          ← server-only (correct)
├── cli/          ← client-only (correct)
├── core/         ← MIXED: server config + database alongside shared utils
├── models/       ← server-only (correct, but in top-level)
├── repository/   ← server-only (correct, but in top-level)
├── search/       ← server-only (correct, but in top-level)
├── storage/      ← server-only (correct, but in top-level)
├── upgrade/      ← server-only (correct, but in top-level)
├── video/        ← client-only (correct, but in top-level)
└── workers/      ← MIXED: client ML providers + server Quickwit worker
```

**Target structure:**
```
src/
├── server/                ← everything the API server needs
│   ├── api/               ← FastAPI app, routers, middleware, dependencies
│   ├── models/            ← SQLModel table definitions
│   ├── repository/        ← data access layer
│   ├── search/            ← Quickwit client, sync, postgres search
│   ├── storage/           ← artifact/proxy file storage
│   ├── upgrade/           ← schema upgrade steps
│   ├── config.py          ← server settings (postgres_dsn, admin_key, etc.)
│   ├── database.py        ← engine, session management
│   └── srt.py             ← SRT parsing (server-only consumer)
├── client/                ← everything the Python CLI needs
│   ├── cli/               ← Typer commands, client HTTP wrapper, config
│   ├── workers/           ← ML providers (CLIP, faces, captions, EXIF)
│   ├── video/             ← video scanning, scene segmentation
│   └── proxy/             ← proxy generation, caching
├── shared/                ← truly shared (must be tiny)
│   ├── path_filter.py     ← glob matching (both sides filter)
│   ├── io_utils.py        ← normalize_path_prefix, file_non_empty
│   ├── asset_status.py    ← status constants
│   ├── file_extensions.py ← IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
│   └── utils.py           ← utcnow
```

**Key moves:**
- `src/core/config.py` + `src/core/database.py` → `src/server/` (server-only)
- `src/core/srt.py` → `src/server/` (only the API parses SRT)
- `src/core/logging_config.py` → `src/client/` (CLI-only, server uses uvicorn logging)
- `src/workers/quickwit.py` → `src/server/search/` (only API uses it)
- `src/workers/embeddings/` → `src/client/workers/embeddings/` (client-side ML)
- `src/workers/faces/` → `src/client/workers/faces/` (client-side ML)
- `src/workers/captions/` → `src/client/workers/captions/` (client-side ML)
- `src/workers/exif_extract.py` → `src/client/workers/` (client-side processing)
- `src/video/` → `src/client/video/` (client-side processing)
- `src/models/`, `src/repository/`, `src/search/`, `src/storage/`, `src/upgrade/` → under `src/server/`
- Remaining `src/core/` utilities → `src/shared/`

**CLIP on the server (similarity endpoint):** The API's `/search-by-image` endpoint currently imports `CLIPEmbeddingProvider` to embed query images server-side. Two options:
1. Add a `/v1/similar-by-vector` endpoint — clients embed locally, send the vector. Server only does pgvector search. The existing `/search-by-image` wraps this: accepts image, embeds server-side, calls vector search. This keeps CLIP as a server dependency for the web UI's sake but makes it optional.
2. Move CLIP to `src/shared/` — rejected, defeats the purpose.

Option 1 is preferred. The server keeps a copy of CLIP for web UI convenience but it's isolated behind one endpoint.

**Entry points update:**
- CLI: `lumiverb = "src.client.cli:main"` (was `src.cli:main`)
- API: `uvicorn src.server.api.main:app` (was `src.api.main:app`)

**pyproject.toml extras update:**
- Base: `src/server/` dependencies (FastAPI, SQLModel, etc.) + CLIP (for similarity endpoint)
- `cli`: `src/client/` dependencies (typer, rich, httpx)
- `workers`: ML dependencies (torch, open-clip, insightface, faster-whisper) — installed alongside `cli` on client machines
- `dev`: everything + test tooling

**Test restructure:**

Current state: 73 test files, 12 with no pytest markers. Tests loosely map to modules but don't enforce the client/server boundary.

Target test structure:
```
tests/
├── server/                ← tests that need the database (slow)
│   ├── test_assets_api.py
│   ├── test_search_endpoint.py
│   ├── ...
├── client/                ← tests that mock the API (fast)
│   ├── test_scan.py
│   ├── test_scan_moves.py
│   ├── ...
├── shared/                ← pure unit tests (fast)
│   ├── test_path_filter.py
│   ├── test_io_utils.py       ← NEW
│   ├── test_file_extensions.py ← NEW
│   ├── test_srt.py
│   ├── ...
├── conftest.py
```

Audit checklist:
- [ ] Every test file has `@pytest.mark.fast` or `@pytest.mark.slow` on every test class/function
- [ ] No test in `tests/client/` imports from `src/server/`
- [ ] No test in `tests/server/` imports from `src/client/`
- [ ] New tests for `src/shared/io_utils.py` (normalize_path_prefix, file_non_empty)
- [ ] New tests for `src/shared/file_extensions.py` (extension lists, media type detection)
- [ ] `tests/shared/test_srt.py` has `@pytest.mark.fast` markers
- [ ] All 73+ test files classified and moved to correct subdirectory
- [ ] Full test suite passes: `uv run pytest tests/`

**Migration strategy:** This is a single high-churn change that touches most files in the repo. Execute as one atomic PR to avoid prolonged merge conflicts. The changes are mechanical (move files, update imports) — not behavioral. Steps:
1. Move files to new locations
2. Find-and-replace all import paths
3. Update `pyproject.toml` entry points and extras
4. Update deploy scripts (`update-vps.sh`, `update-api.sh`) with new entry points
5. Run full test suite, fix any import breakage
6. Verify deploy scripts work against a test environment
7. Audit `docs/cursor-api.md` for any undocumented API behavior that clients rely on implicitly (auth-gated image downloads, thumbnail URL patterns, pagination edge cases) — document anything missing before Phase 1 begins

**Does NOT include:** Any Swift code, Xcode project, or CoreML models. This phase is purely Python reorganization.

**Read-ahead:** Phase 1 creates the Swift project under `clients/lumiverb-app/`. The API contract in `docs/cursor-api.md` becomes the sole interface between `src/server/` and all clients.

**Done when:**
- [ ] All source files moved to `src/server/`, `src/client/`, or `src/shared/`
- [ ] All imports updated, no cross-boundary violations
- [ ] Entry points updated in `pyproject.toml`
- [ ] All tests passing (`uv run pytest tests/`)
- [ ] TypeScript compiles (`npx tsc --noEmit`), Vite builds (`npx vite build`)
- [ ] Test files reorganized into `tests/server/`, `tests/client/`, `tests/shared/`
- [ ] Every test marked `fast` or `slow`
- [ ] Missing tests added (io_utils, file_extensions)
- [ ] `docs/cursor-cli.md` and `docs/cursor-api.md` updated with new import paths
- [ ] `docs/cursor-api.md` audited: all implicit client dependencies documented explicitly
- [ ] Deploy scripts verified (`update-vps.sh`, `update-api.sh` use correct entry points)

### Phase 1 — Swift Project Scaffold

Create the Xcode project, shared Swift package, API client, and authentication flow. No UI beyond a login screen and "connected" confirmation.

**Deliverables:**
- Xcode project at `clients/lumiverb-app/` with macOS and iOS targets
- `Shared/` Swift package with:
  - `APIClient` — URLSession wrapper mirroring `LumiverbClient` (base URL, auth header, error handling)
  - `AuthManager` — JWT login flow + token refresh + secure keychain storage
  - Swift types for core API responses: `Library`, `AssetPageItem`, `SearchHit`, `CurrentUser`
- macOS target: menu bar icon, login window, "Connected to {api_url}" confirmation
- iOS target: login screen, same confirmation
- CI: `xcodebuild` in GitHub Actions for both targets

**Does NOT include:** Media grid, search, file watching, ML inference, or any processing.

**Read-ahead:** Phase 2 needs paginated asset fetching and image loading. Ensure `APIClient` supports cursor-based pagination and auth-gated image downloads (thumbnail/proxy URLs require the `Authorization` header — the web UI uses `useAuthenticatedImage` for this; the Swift client needs the equivalent). Verify that `docs/cursor-api.md` documents the thumbnail/proxy download endpoints explicitly, including auth requirements and URL patterns.

**Done when:**
- [ ] Both targets build and run
- [ ] Login flow works against a real Lumiverb server (JWT login → access token → refresh)
- [ ] Token stored in Keychain, refresh handled transparently
- [ ] API client can list libraries
- [ ] Swift unit tests for API client (mock URLProtocol)
- [ ] CI green for both targets

### Phase 2 — macOS Browse Shell

The macOS app becomes a usable media browser — equivalent to the web UI's browse and search capabilities.

**Deliverables:**
- Sidebar: library list with selection
- Content area: virtualized media grid (`LazyVGrid` with async thumbnail loading)
- Cursor-based infinite scroll (matching web UI's TanStack Virtual approach)
- Lightbox: proxy image display + metadata sidebar (EXIF, faces, tags)
- Search: text search with results grid
- Similar: find-similar-by-asset
- Keyboard navigation (arrow keys in grid, Escape to close lightbox)

**Does NOT include:** File watching, scanning, enrichment, face tagging, collections. macOS only (iOS in Phase 5).

**Read-ahead:** Phase 3 adds background processing. Ensure the UI architecture supports a status indicator in the menu bar that updates while the user browses.

**Done when:**
- [ ] Can browse all libraries with smooth scrolling on 20K+ asset libraries
- [ ] Search returns results with correct hit types (image, scene, transcript)
- [ ] Lightbox shows proxy image + full metadata
- [ ] Performance: thumbnail grid loads as fast as web UI or faster
- [ ] Swift tests for view models and pagination logic

### Phase 3 — macOS Background Scan

The macOS app watches library directories and automatically scans for new/changed/moved/deleted files. Equivalent to `lumiverb scan` running continuously.

**Deliverables:**
- `FSEvents` file system watcher on all library root paths
- Scan pipeline: discover → mtime+size check → SHA hash → EXIF extract → proxy generate → upload
- Move detection (SHA match against deleted server assets, same as Python CLI)
- Deletion detection (server assets not on disk)
- Menu bar status: "Watching 3 libraries", "Processing 4 of 127 new files", "Idle"
- Pause/resume processing from menu bar
- Settings: configure library paths, concurrency, scan interval
- Proxy cache (local cache of generated proxies, same SHA sidecar approach)

**Does NOT include:** ML enrichment (CLIP, faces, OCR, transcription). That's Phase 4.

**Read-ahead:** Phase 4 adds CoreML inference. Ensure the scan pipeline produces the same proxy format (2048px JPEG/WebP) that CoreML models expect as input.

**Done when:**
- [ ] File changes detected within seconds via FSEvents
- [ ] New files: proxy generated and uploaded automatically
- [ ] Changed files: re-scanned (mtime+size fast check, SHA thorough check)
- [ ] Moved files: detected and applied (or prompted, matching CLI behavior)
- [ ] Deleted files: soft-deleted on server
- [ ] Processing survives volume mount/unmount (NAS disconnect/reconnect)
- [ ] Menu bar shows accurate progress with item counts
- [ ] Swift tests for scan pipeline logic (mocked API)

### Phase 4 — macOS Enrichment

The macOS app runs ML inference on scanned assets. Equivalent to `lumiverb enrich`.

**Deliverables:**
- CoreML CLIP model conversion scripts (Python → ONNX → CoreML) + validation test set
- CoreML ArcFace model conversion scripts + validation
- CLIP embedding generation (CoreML, Neural Engine acceleration)
- Face detection (Apple Vision `VNDetectFaceRectanglesRequest`) + face embedding (CoreML ArcFace)
- OCR provider pattern: Apple Vision `VNRecognizeTextRequest` (default) + OpenAI-compatible API (configurable)
- Vision AI: OpenAI-compatible API for captions/tags (same as Python CLI)
- Transcription: whisper.cpp integration (linked library or subprocess), configurable model size
- Settings: OCR provider toggle, whisper model selection, vision API config
- Enrichment runs automatically after scan, or on-demand for specific assets

**Does NOT include:** iOS enrichment (iOS is browse-only). Windows support.

**Read-ahead:** Phase 5 shares the browse UI with iOS. Ensure all enrichment code is macOS-target only and doesn't leak into the shared package.

**Done when:**
- [ ] CLIP embeddings match Python output (cosine sim >0.999 on 100-image test set)
- [ ] Face detection + embedding matches Python quality (precision/recall within 5% on test set)
- [ ] OCR produces comparable text extraction to API-based approach
- [ ] Transcription produces valid SRT, submitted to server correctly
- [ ] All enrichment types can be paused/resumed independently
- [ ] Processing respects concurrency settings
- [ ] Swift tests for each provider (mocked CoreML for unit tests, real models for integration)
- [ ] CoreML model files checked into `clients/lumiverb-app/CoreML/`
- [ ] Conversion scripts checked into `scripts/convert-models/`

### Phase 5 — iOS Browse App

Adapt the shared SwiftUI browse UI for iOS. No processing — pure API client.

**Deliverables:**
- iOS app target using shared views from Phase 2
- Platform adaptations: tab bar navigation (vs sidebar), touch gestures, pull-to-refresh
- Search with keyboard avoidance
- Lightbox with swipe gestures
- Similar-by-image using camera roll (capture photo → upload to `/search-by-image`)
- Download proxy to camera roll (share sheet)
- Respect iOS low-data mode (skip proxy preloading)
- App icon, launch screen

**iOS distribution:** TestFlight for internal testing, App Store for public release. The iOS app is sandboxed (App Store requirement) but this is fine — it only needs network access to talk to the API server. No file system access, no background processing.

**Entitlements required:**
- `com.apple.security.network.client` — outbound network (API server)
- Photo Library access — save proxies to camera roll (user-prompted)
- Camera access — capture photo for similar-by-image (user-prompted)

**Does NOT include:** File watching, scanning, enrichment, face tagging, or collections.

**Read-ahead:** Phase 6 adds face tagging to both platforms. Ensure the lightbox face overlay architecture in the shared package supports touch (iOS) and click (macOS) interactions.

**Done when:**
- [ ] All browse/search/lightbox features work on iPhone and iPad
- [ ] Thumbnails load efficiently on cellular
- [ ] App submitted to TestFlight for internal testing
- [ ] Swift tests for iOS-specific adaptations

### Phase 6 — Face Tagging (Both Platforms)

Face tagging UI shared between macOS and iOS, adapted for each platform's input model.

**Deliverables:**
- Face overlay on lightbox proxy image (bounding boxes from API)
- Tap/click face → assign to person (search existing, create new)
- Person merge (combine duplicates)
- Person browse: grid of all people, tap to see their photos
- Dismiss false-positive detections
- Shared SwiftUI components, platform-specific gesture handling

**Does NOT include:** Face re-detection or embedding generation on iOS (that's macOS-only enrichment). iOS displays face bounding boxes and embeddings that were produced by macOS enrichment or the Python CLI and stored on the server. iOS never needs CoreML ArcFace or Apple Vision face detection — it only reads face data from the API and writes back person assignments.

**Done when:**
- [ ] Face tagging works on both macOS and iOS
- [ ] Person assignment, creation, merge all functional
- [ ] Dismiss works correctly (face excluded from future matching)
- [ ] Swift tests for face tagging view models

## Alternatives Considered

**Flutter (Dart):** Cross-platform with Windows support built in. Rejected because: (1) ML inference ecosystem less mature — ONNX/CoreML FFI is possible but clunky; (2) file system watching and background processing require extensive platform channels; (3) UI never feels fully native on macOS; (4) Dart is a new language with less ecosystem depth than Swift for Apple-specific APIs (Vision, ImageIO, CryptoKit, FSEvents).

**React Native + React Native macOS:** Leverages existing React/TypeScript skills. Rejected because: (1) macOS support is a Microsoft-maintained fork, not first-class; (2) background processing model is fundamentally weak — RN is a UI framework; (3) ML inference requires native modules for everything; (4) file system access is limited by design.

**Electron:** Wraps the existing web UI in a desktop shell. Rejected because: (1) no iOS support; (2) background processing is possible but resource-heavy (Chromium + Node.js running permanently); (3) defeats the purpose of a native experience; (4) 200MB+ app size for a web wrapper.

**Keep Python CLI + enhance web UI:** Zero new language/framework investment. Rejected because: (1) can't do background file watching from a web browser; (2) no iOS app possible; (3) installation friction of Python + uv + ML dependencies remains; (4) user explicitly wants native experience.

**SwiftUI + separate Windows app later:** This is what we chose. The tradeoff is that Windows will require a separate implementation (likely Flutter or WinUI 3). Accepted because: (1) Apple platforms are the priority; (2) SwiftUI gives the best native experience on macOS + iOS; (3) Windows can be addressed when there's demand; (4) the API contract is the coupling point, not shared UI code.

## What This Does NOT Include

- **Windows client** — future ADR when there's demand. The API contract and provider patterns established here will inform the approach.
- **Android client** — no current plan.
- **Server-side processing** — inference stays client-side (privacy-first architecture). The server stores results, not source files.
- **Python CLI deprecation** — the CLI remains for scripting, batch operations, and platforms without native clients.
- **Real-time sync** — the native app polls/watches locally and pushes to the server. No WebSocket push from server to client. If another client modifies data, the native app sees it on next API fetch.
- **P2P sync between clients** — all sync goes through the server API.
- **Offline mode** — the native app requires server connectivity. Local processing (scan, enrich) queues uploads for when connectivity returns, but browse/search require the server.
