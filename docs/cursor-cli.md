# Lumiverb CLI — Cursor Context
*Feed this to Cursor when working on the CLI.*

## Purpose
The CLI is a local agent that runs on the machine where source files live.
It never touches the tenant DB, Quickwit, or object storage directly — it is an API client only.

See docs/architecture.md for the full design.

## Package layout
- `src/cli/main.py` — Typer app entry point; command groups: `config`, `library`, and top-level `scan`
- `src/cli/config.py` — Local config in `~/.lumiverb/config.json` (`api_url`, `api_key`): `load_config`, `save_config`, `get_api_url`, `get_api_key`
- `src/cli/client.py` — `LumiverbClient`: thin httpx wrapper, reads config for base URL and `Authorization: Bearer <api_key>`; on non-2xx prints error envelope and exits 1
- `src/cli/progress.py` — `UnifiedProgress`: unified layout (spinner + bar + N/M units + counters) for all long-running commands (scan, workers, search-sync). Disabled when not a terminal.

Entry point: `lumiverb = "src.cli:main"` (setuptools); `main()` invokes the Typer app.

## Commands
- `lumiverb config set --api-url <url> --api-key <key>` — write config
- `lumiverb config show` — show api_url and whether api_key is set
- `lumiverb library create <name> <path>` — POST /v1/libraries
- `lumiverb library list` — GET /v1/libraries (Rich table: ID, Name, Root path, Scan status, Vision Model, Last scan; trashed libraries hidden)
- `lumiverb library set-model <library_id> <model>` — PATCH vision_model_id. Use `moondream` for local Moondream; any other string for OpenAI-compatible API (via VISION_API_URL).
- `lumiverb library delete <name>` — Soft delete: move library to trash (prompt for confirmation)
- `lumiverb library empty-trash` — Permanently delete all trashed libraries and their assets (prompt for confirmation)
- `lumiverb status --library <name>` — Show pipeline status: asset counts by stage (proxy, EXIF, vision, search sync) with done/pending/failed breakdown.
- `lumiverb failures --library <name> --job-type <type> [--path <prefix>] [--limit N]` — List failed jobs with error messages. Shows most recent failure per asset. Prints retry command hint.
- `lumiverb scan --library <name> [--path <subpath>] [--force]` — Scan a library for media files; discovers/upserts assets via API, reports added/updated/skipped/missing.
- `lumiverb enqueue --library <name> [--job-type proxy|exif|ai_vision|embed] [--path <path>] [--asset <id>] [--since <iso>] [--until <iso>] [--missing-proxy] [--missing-thumbnail] [--force] [--retry-failed]` — Enqueue processing jobs for a library. Short form: `-l <name>`. `--retry-failed` re-enqueues only assets with failed jobs (mutually exclusive with `--force`). `embed` enqueues assets that have a proxy but no embeddings yet.
- `lumiverb search --library <name> <query> [--output table|json|text] [--limit N] [--offset N]` — Search assets in a library by natural language query. Default output: Rich table. `--limit 0` fetches all results (paginated). Short form: `-l <name>`, `-o` for output.
- `lumiverb similar --library <name> <asset_id> [--limit N] [--offset N] [--output table|json|text]` — Find visually similar assets by vector similarity. Default limit 10. Short form: `-l <name>`, `-o` for output.
- `lumiverb worker embed [--library <name>] [--once]` — Run the embedding worker (CLIP + Moondream vectors for similarity search). Use `-l <name>` to scope to one library; `--once` processes the queue until empty then exits.
- `lumiverb worker search-sync --library <name> [--once] [--path <subpath>] [--force-resync]` — Run the search sync worker. Drains search_sync_queue, indexes asset metadata to Quickwit (if enabled), falls back gracefully if Quickwit is unavailable. `--path` scopes sync to a subfolder. `--force-resync` re-enqueues all assets regardless of prior sync status. Shows progress and summary table on completion.

Shell alias (one-shot sync): `function lumi-search-sync() { lumiverb worker search-sync --library "$1" --once; }`

Output: Rich tables for list; green success for create; errors handled by client (stderr + exit 1).
