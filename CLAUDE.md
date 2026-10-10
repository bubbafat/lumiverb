# Claude Code — Lumiverb repository guide

Navigation index. Read this before `ls`/`grep`/`find`. File paths and
ownership live here; logic, schemas, and thresholds do not — read the code.

---

## Stack & topology

Python 3.12 (FastAPI + SQLModel + Postgres 18 + Quickwit), React 18 +
Vite + TanStack web UI, Swift 5.9 / SwiftUI / CoreML / Vision for native
macOS + iOS (shared `LumiverbKit` package, XcodeGen project).

Today FastAPI is the only persistent service. CLI + macOS run local
enrichment and POST results back; iOS is browse-only. Direction is
ADR-016: a Linux "brain" schedules all processing against read-only
storage, human data is never overwritten by derived data, and what used
to be "collections" are projects (`/v1/projects` for every client;
`/v1/collections` is gone). Quickwit sidecar with a
Postgres fallback. Auth: JWT (web) + API keys (CLI), both
`Authorization: Bearer <token>`; roles `admin` / `editor` / `viewer`.

---

## Top-level layout

```
src/
  server/                  Python API server, DB, search, upgrade runner, scheduler (all processing)
  client/                  Python CLI + the processing steps the scheduler runs
  shared/                  Code used by both server and client
  ui/web/                  React web app (Vite)

  api/ cli/ core/ models/ repository/ search/ storage/ upgrade/ video/ workers/
                           ⚠️  STALE pre-ADR-014 __pycache__ shells. Ignore.

clients/lumiverb-app/      Native macOS + iOS (XcodeGen)
  Sources/macOS/  iOS/  LumiverbKit/      project.yml, Lumiverb.xcodeproj

tests/        Python tests (pytest)
migrations/   Alembic — control/ + tenant/ trees
scripts/      Ops (update.sh = one-command update, logged; deploy-*, update-*, migrate.sh, convert-models/, …)
docs/         Long-form docs + ADRs
quickwit/     Quickwit index schemas
```

---

## Finding things by topic

| Change… | Look in |
|---|---|
| API endpoint | `src/server/api/routers/<area>.py` (one file per domain) |
| DB tables | `src/server/models/tenant.py` (big one) + `models/control_plane.py` |
| DB query / repository | `src/server/repository/tenant.py` — ~13 `<Domain>Repository` classes |
| FastAPI entry / deps | `src/server/api/main.py`, `api/dependencies.py` (`require_admin`, `require_tenant_admin`, `require_editor`) |
| Auth / tenant resolution | `src/server/api/middleware.py` |
| Token persistence (Swift clients) | `LumiverbKit/Auth/{AuthManager,KeychainHelper,FileTokenStore}.swift`. `TokenStore` protocol with two implementations: `KeychainHelper` (Security framework `kSecClassGenericPassword`) and `FileTokenStore` (`~/Library/Application Support/io.lumiverb.app/credentials.json`, mode 0600). **macOS defaults to `FileTokenStore`** because the legacy macOS keychain prompts the user per-access for ad-hoc dev builds (rebuilds rotate binary identity, busting any "Always Allow" ACL). **iOS uses `KeychainHelper`** explicitly — iOS keychain is data-protection by default and never prompts. `AuthManager` caches the token in memory after first read so refresh calls don't re-hit the store. |
| DDL migrations | `migrations/control/versions/`, `migrations/tenant/versions/`. Run via `scripts/migrate.sh` |
| Online data backfills | `src/server/upgrade/` (`steps/`, `registry.py`, `runner.py`). For long-running migrations that can't run in one Alembic txn |
| Video transcription | Client: `src/client/cli/repair.py` `_transcribe_one` (ffmpeg from the analysis proxy → the speech found here, `workers/transcripts/speech.py` → a machine doing the transcripts job hears it: the built-in faster-whisper `local.py` or a server `openai_compatible.py`, pooled by `cli/transcript_guard.py`). Server: `api/routers/assets.py`, `server/srt.py`. Index `lumiverb_{tenant}_transcripts` |
| Producers (what each makes, from what, how it runs) | One folder each: `src/producers/<artifact>/__init__.py` declares a `ProducerSpec` (`src/producers/contract.py`); the registry is `src/producers/__init__.py`. Read from it: `shared/producers.py` (lineage rules), `repository/lineage.py` (reconciler), `server/scheduler/kinds.py`, `runners.py`, `service.py` (pools), `routers/producers.py`. Still named by hand: the repair summary and page `missing_*` filters (`routers/assets.py`, `repository/tenant.py`), a new AI job (`shared/ai_jobs.py` + a guard) |
| AI machines and jobs | Server: `api/routers/ai.py`, `repository/ai_machines.py`; jobs `src/shared/ai_jobs.py`, Whisper names `src/shared/whisper_models.py`. Worker: `cli/ai_pool.py`, `cli/job_guard.py` (+ `vision_guard.py`, `transcript_guard.py`). Web: `pages/settings/AiSection.tsx` |
| The brain: all processing (the scheduler) | `src/server/scheduler/` (`lumiverb-scheduler.service`: `kinds.py` tiers and pools, `queue.py` what's due from the database, `dispatch.py` which job runs next, `runners.py` one job of each kind, `scans.py` reported changes and daily full scans), `src/client/cli/roots.py` (library roots mapped to this machine), `routers/changes.py` (change reports from the Mac). Setup and ports: `docs/brain-setup.md` |
| Analysis proxies | Render: `src/client/video/analysis_proxy.py`. Cache: `src/client/proxy/analysis_cache.py`. Stored as the `analysis_proxy` artifact (`routers/artifacts.py`) |
| Filter algebra | `src/server/models/query_filter.py` (23 leaf types), `filter_registry.py` (parsing + capabilities). Web: `src/ui/web/src/lib/queryFilter.ts`. Swift: `LumiverbKit/Models/QueryFilter.swift` |
| Unified query endpoint | `src/server/api/routers/query.py` — single `GET /v1/query?f=prefix:value` replaces old `/v1/browse` + `/v1/search`. Candidate-set pattern for text search (Quickwit → Postgres filters) |
| Filter capabilities | `src/server/api/routers/filters.py` — `GET /v1/filters/capabilities` returns all filter types for generic client rendering |
| Search (Quickwit) | `src/server/search/quickwit_client.py`, `sync.py`, `cleanup.py`, `postgres_search.py` (ILIKE fallback) |
| CLI command | `src/client/cli/<command>.py`. Dispatcher: `main.py`. HTTP: `client.py` (`LumiverbClient`) |
| Face detection (Python) | `src/client/workers/faces/insightface_provider.py` (InsightFace buffalo_l) |
| Face detection (macOS/iOS) | `LumiverbKit/Sources/LumiverbKit/Faces/FaceDetectionProvider.swift` (Vision). Lives in LumiverbKit so tests drive the full gate chain end-to-end. Callers: `Sources/macOS/Enrich/{EnrichmentPipeline,ReEnrichmentRunner}.swift` |
| ArcFace (macOS) | `Sources/macOS/Enrich/ArcFaceProvider.swift`; CoreML built by `scripts/convert-models/convert_arcface.py` |
| Whisper transcription (macOS) | `Sources/macOS/Enrich/WhisperProvider.swift` (orchestrator). Audio extraction + subprocess wrapper live in LumiverbKit so tests drive the pipeline end-to-end: `LumiverbKit/Sources/LumiverbKit/Audio/{AudioExtraction,WhisperRunner}.swift`. UX: opt-in toggle in Settings, model auto-downloaded on Save (`Sources/macOS/Enrich/WhisperModelManager.swift` + `WhisperDownloadSheet.swift`). Single-active-model invariant — old models cleaned up on size change. Requires `brew install whisper-cpp`. Fixture tests at `Tests/LumiverbKitTests/Fixtures/transcribe-*.mov` (English / Spanish / silence / music). `WhisperRunner.sanitizeSRT` strips whisper's `[BLANK_AUDIO]` placeholders and IPA-glyph silence-loop hallucinations (a known whisper.cpp behavior on near-silent input + macOS AAC decoder dithering). |
| Enrichment orchestration (macOS) | `Sources/macOS/Enrich/` |
| Scan orchestration (macOS) | `Sources/macOS/Scan/{ScanState,ScanPipeline,LibraryWatcher}.swift`. `ScanState` is the @MainActor coordinator (persistent pause via `UserDefaults("scanPaused")`, `pendingRescan` to catch mid-scan FSEvents). `LibraryWatcher` uses leading-schedule debounce — first event in a quiet window schedules a fire, subsequent events do NOT reset the timer (intentional, prevents pathological writers from starving the queue). `ScanPipeline.discoverFiles()` applies a 30s mtime quarantine so half-written files (renders, copies) get picked up on a later pass. Initial scan kicks on `startWatching()`. |
| Menu bar / app lifecycle (macOS) | `Sources/macOS/{LumiverbApp,MenuBarView}.swift`. 3-state SF symbol: `pause.rectangle.fill` (paused) / `arrow.triangle.2.circlepath` (scanning) / `photo.stack` (idle). Favorites (`AppState.favoriteLibraryIds`, persisted) surface in the menu bar; clicking sets `appState.pendingSelectedLibraryId` which `BrowseWindow.consumePendingLibraryId()` consumes on appear/onChange. Start at Login via `SMAppService.mainApp` toggle in Settings (requires app in `/Applications` to actually fire). |
| Proxy gen / cache (Python) | `src/client/proxy/proxy_gen.py`, `proxy_cache.py` |
| Image caches (macOS) | `LumiverbKit/Sources/LumiverbKit/API/ImageCache.swift` (NSCache in-memory, 200 MB / 2000 items), `ProxyCacheOnDisk.swift` (`~/.cache/lumiverb/proxies/`, Python-CLI-compatible, SHA sidecars), `ThumbnailCacheOnDisk.swift` (`~/.cache/lumiverb/thumbnails/`, macOS-local, no sidecar). `AuthenticatedImageView` resolves in-memory → disk → server, on a detached Task so disk I/O and `NSImage(data:)` decode stay off the main actor. |
| Web UI page | `src/ui/web/src/pages/<Page>.tsx`. API client: `src/ui/web/src/api/client.ts`. Filter state: `src/ui/web/src/lib/queryFilter.ts` |
| Face clustering / people | `repository/tenant.py` (`PersonRepository`, `FaceRepository`); `routers/people.py`. Cluster cache in `system_metadata`, invalidated on writes |
| Python ↔ Swift shared | `src/shared/` — twins in `LumiverbKit/Sources/LumiverbKit/Models/`: `path_filter.py` ↔ `PathFilter.swift`, `file_extensions.py` ↔ `FileExtensions.swift` |
| Server config / env | `src/server/config.py` (`Settings(BaseSettings)`). CLI: `src/client/cli/config.py`. Prod env: `/etc/lumiverb/env` |

---

## Running things

Standard incantations work (`uvicorn src.server.api.main:app --reload`,
`lumiverb <cmd>`, `npm run dev`, `swift test`). Non-obvious:

- **uv only** — no `pip`. Use `uv add` / `uv run` / `uv sync`.
- **macOS app** — `cd clients/lumiverb-app && xcodegen generate && xcodebuild -project Lumiverb.xcodeproj -scheme Lumiverb-macOS -configuration Debug build CODE_SIGNING_ALLOWED=NO`
- **ArcFace convert** — `scripts/convert-models/` has its own venv

---

## Tests

- Python: `uv run pytest -m fast` (no DB/AI), `-m slow` (testcontainers Postgres 18, `PG_IMAGE` in `tests/conftest.py`), `ai` (real inference, opt-in). Bare `pytest` runs fast + slow.
  `-n auto` runs it in parallel (pytest-xdist, a module's tests on one worker). One Postgres serves the whole run: `PostgresContainer` in a test is a fresh database on it, and the migration helpers clone a template migrated once (`tests/conftest.py` docstring; `LUMIVERB_TEST_PG_PER_MODULE=1` for a container per module).
  Quickwit is off for every test (conftest pins `QUICKWIT_ENABLED=false`, whatever `.env.local` says; mock `QuickwitClient` to test sync code). `uv run pytest -m quickwit` (opt-in) checks the real document builders against the real index schemas and search in a throwaway Quickwit in Docker (`tests/test_quickwit_real.py`).
- Swift: XCTest in `LumiverbKit`. **No macOS-target test bundle** — testable code must live in LumiverbKit.
- Web UI: `npx vitest run` in `src/ui/web` (Testing Library component tests sit beside their components), plus `npx tsc --noEmit -p .`.

---

## API conventions (do not relitigate)

- All routes under `/v1/`, `Bearer` token required.
- Tenant from token, **never** a URL param.
- Cursor pagination only (`after` / `next_cursor`).
- Errors: `{"error": {"code", "message", "details"}}`.
- Multipart uploads. No webhooks, no source serving, no rate limiting.
- Routes match in **definition order** — static before parameterized.
- **The API requires the user's say** for surprising outcomes: the request carries the choice (e.g. `remove_from_projects`, `with_clips`) or gets a 409 in the error envelope with the facts, so every client must ask (`api/errors.py` `DecisionRequiredError`; cursor-api.md "Decisions the API requires").
- Deleting goes delete → trash → restore | delete forever, for assets and projects (libraries have a trash, no restore yet).

---

## Gotchas

- **`active_assets` view** must be dropped + recreated when adding columns to `assets`.
- **ONNX Runtime leaks ~35 MB/inference.** Batch work is subprocess-isolated for exactly this reason — don't collapse it.
- **`ThreadPoolExecutor`**: never submit all futures at once; cap at `concurrency * 2` and `_drain()`.
- **`systemd` units** are not regenerated by `update-vps.sh` after entry-point changes — fix manually.
- **Vision `VNDetectFaceCaptureQualityRequest` is not deterministic** across runs — the same face can score ~0.02 apart in different processes because Vision dispatches across CPU/GPU/ANE based on system load. Don't calibrate thresholds against a single observed value, and don't write count-based face-detection tests on fixtures with subjects sitting near the gate (`face_crowd.jpg`). Leave ≥ 0.05 headroom; assert *existence* not *count* on borderline fixtures.
- **Prod**: never SSH or run commands on the VPS. User handles deploys; server auto-updates after push.

---

## Key docs

- `docs/adr/016-operating-model.md` — **direction**. Wins over other docs on conflict.
- `docs/cursor-api.md` — **authoritative** API reference. Do not relitigate.
- `docs/architecture.md` — full system design.
- Everything else: `ls docs/` and `ls docs/adr/`.
