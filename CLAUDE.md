# Claude Code — Lumiverb repository guide

Navigation index. Read this before `ls`/`grep`/`find`. File paths and
ownership live here; logic, schemas, and thresholds do not — read the code.

---

## Stack & topology

Python 3.12 (FastAPI + SQLModel + Postgres 16 + Quickwit), React 18 +
Vite + TanStack web UI, Swift 5.9 / SwiftUI / CoreML / Vision for native
macOS + iOS (shared `LumiverbKit` package, XcodeGen project).

FastAPI is the only persistent service. CLI + macOS run local enrichment
and POST results back; iOS is browse-only. Quickwit sidecar with a
Postgres fallback. Auth: JWT (web) + API keys (CLI), both
`Authorization: Bearer <token>`; roles `admin` / `editor` / `viewer`.

---

## Top-level layout

```
src/
  server/                  Python API server, DB, search, upgrade runner
  client/                  Python CLI + worker processes
  shared/                  Code used by both server and client
  ui/web/                  React web app (Vite)

  api/ cli/ core/ models/ repository/ search/ storage/ upgrade/ video/ workers/
                           ⚠️  STALE pre-ADR-014 __pycache__ shells. Ignore.

clients/lumiverb-app/      Native macOS + iOS (XcodeGen)
  Sources/macOS/  iOS/  LumiverbKit/      project.yml, Lumiverb.xcodeproj

tests/        Python tests (pytest)
migrations/   Alembic — control/ + tenant/ trees
scripts/      Ops (deploy-*, update-*, migrate.sh, convert-models/, …)
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
| DDL migrations | `migrations/control/versions/`, `migrations/tenant/versions/`. Run via `scripts/migrate.sh` |
| Online data backfills | `src/server/upgrade/` (`steps/`, `registry.py`, `runner.py`). For long-running migrations that can't run in one Alembic txn |
| Video transcription | Client: `src/client/cli/repair.py` (ffmpeg → faster-whisper). Server: `api/routers/assets.py`, `server/srt.py`. Index `lumiverb_{tenant}_transcripts` |
| Search (Quickwit) | `src/server/search/quickwit_client.py`, `sync.py`, `cleanup.py`, `postgres_search.py` (fallback) |
| CLI command | `src/client/cli/<command>.py`. Dispatcher: `main.py`. HTTP: `client.py` (`LumiverbClient`) |
| Face detection (Python) | `src/client/workers/faces/insightface_provider.py` (InsightFace buffalo_l) |
| Face detection (macOS/iOS) | `LumiverbKit/Sources/LumiverbKit/Faces/FaceDetectionProvider.swift` (Vision). Lives in LumiverbKit so tests drive the full gate chain end-to-end. Callers: `Sources/macOS/Enrich/{EnrichmentPipeline,ReEnrichmentRunner}.swift` |
| ArcFace (macOS) | `Sources/macOS/Enrich/ArcFaceProvider.swift`; CoreML built by `scripts/convert-models/convert_arcface.py` |
| Whisper transcription (macOS) | `Sources/macOS/Enrich/WhisperProvider.swift` (orchestrator). Audio extraction + subprocess wrapper live in LumiverbKit so tests drive the pipeline end-to-end: `LumiverbKit/Sources/LumiverbKit/Audio/{AudioExtraction,WhisperRunner}.swift`. UX: opt-in toggle in Settings, model auto-downloaded on Save (`Sources/macOS/Enrich/WhisperModelManager.swift` + `WhisperDownloadSheet.swift`). Single-active-model invariant — old models cleaned up on size change. Requires `brew install whisper-cpp`. Fixture tests at `Tests/LumiverbKitTests/Fixtures/transcribe-*.mov` (English / Spanish / silence / music). `WhisperRunner.sanitizeSRT` strips whisper's `[BLANK_AUDIO]` placeholders and IPA-glyph silence-loop hallucinations (a known whisper.cpp behavior on near-silent input + macOS AAC decoder dithering). |
| Enrichment orchestration (macOS) | `Sources/macOS/Enrich/` |
| Proxy gen / cache (Python) | `src/client/proxy/proxy_gen.py`, `proxy_cache.py` |
| Web UI page | `src/ui/web/src/pages/<Page>.tsx`. API client: `src/ui/web/src/api/client.ts` |
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

- Python: `uv run pytest -m fast` (no DB/AI), `-m slow` (testcontainers Postgres), `ai` (real inference, opt-in). Bare `pytest` runs fast + slow.
- Swift: XCTest in `LumiverbKit`. **No macOS-target test bundle** — testable code must live in LumiverbKit.
- Web UI: no component render tests yet.

---

## API conventions (do not relitigate)

- All routes under `/v1/`, `Bearer` token required.
- Tenant from token, **never** a URL param.
- Cursor pagination only (`after` / `next_cursor`).
- Errors: `{"error": {"code", "message", "details"}}`.
- Multipart uploads. No webhooks, no source serving, no rate limiting.
- Routes match in **definition order** — static before parameterized.

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

- `docs/cursor-api.md` — **authoritative** API reference. Do not relitigate.
- `docs/architecture.md` — full system design.
- Everything else: `ls docs/` and `ls docs/adr/`.
