# Lumiverb CLI — Cursor Context
*Feed this to Cursor when working on the CLI.*

## Purpose
The CLI is a local agent that runs on the machine where source files live.
It never touches the tenant DB, Quickwit, or object storage directly — it is an API client only.

See docs/architecture.md for the full design.

## Package layout
- `src/cli/main.py` — Typer app entry point; command groups: `config`, `library`, `tenant`, and top-level `scan`
- `src/cli/config.py` — Local config in `~/.lumiverb/config.json` (`api_url`, `api_key`, `admin_key`): `load_config`, `save_config`, `get_api_url`, `get_api_key`, `get_admin_key`
- `src/cli/client.py` — `LumiverbClient`: thin httpx wrapper, reads config for base URL and `Authorization: Bearer <api_key>`; accepts `api_key_override` for admin commands; on non-2xx prints error envelope and exits 1
- `src/cli/progress.py` — `UnifiedProgress`: unified layout (spinner + bar + N/M units + counters) for all long-running commands (scan, workers, search-sync). Disabled when not a terminal.

Entry point: `lumiverb = "src.cli:main"` (setuptools); `main()` invokes the Typer app.

## Commands
- `lumiverb config set [--api-url <url>] [--api-key <key>] [--admin-key <key>]` — write config
- `lumiverb config show` — show api_url, whether api_key is set, whether admin_key is set
- `lumiverb library create <name> <path>` — POST /v1/libraries
- `lumiverb library list` — GET /v1/libraries (Rich table: ID, Name, Root path, Scan status, Vision Model, Last scan; trashed libraries hidden)
- `lumiverb library set-model --library <name> --model <model_id>` — PATCH vision_model_id. Model ID is any OpenAI-compatible model name (e.g. `qwen3-visioncaption-2b`). Vision API URL and key are set at the tenant level.
- `lumiverb library delete <name>` — Soft delete: move library to trash (prompt for confirmation)
- `lumiverb library empty-trash` — Permanently delete all trashed libraries and their assets (prompt for confirmation)
- `lumiverb tenant list [--admin-key <key>]` — List all tenants (tenant_id, name, plan, status). Admin key falls back to saved config.
- `lumiverb tenant set-vision --tenant-id <id> [--vision-api-url <url>] [--vision-api-key <key>] [--admin-key <key>]` — Set the OpenAI-compatible vision API URL and/or key for a tenant. Stored on the tenant record; passed to workers automatically via job payload. Admin key falls back to saved config.
- `lumiverb status --library <name>` — Show pipeline status: asset counts by stage (proxy, EXIF, vision, search sync) with done/pending/failed breakdown.
- `lumiverb failures --library <name> --job-type <type> [--path <prefix>] [--limit N]` — List failed jobs with error messages. Shows most recent failure per asset. Prints retry command hint.
- `lumiverb scan --library <name> [--path <subpath>] [--force]` — Scan a library for media files; discovers/upserts assets via API, reports added/updated/skipped/missing.
- `lumiverb admin maintenance` — Show current maintenance mode status (`active`, `message`, `started_at`).
- `lumiverb admin maintenance --start [--message "..."]` — Enable maintenance mode; workers stop claiming jobs immediately.
- `lumiverb admin maintenance --end` — Disable maintenance mode; workers resume normally.
- `lumiverb upgrade [--dry-run] [--max-steps N] [--step <step_id>] [--force]` — Run tenant-level upgrade steps (schema/backfills) idempotently. **Requires maintenance mode to be active** (enforced at runtime; `--dry-run` bypasses this check). With `--dry-run`, lists pending steps without executing. With `--step`, run only a single step; by default it refuses to run if preceding steps are not complete unless `--force` is provided (with a confirmation prompt).
- `lumiverb enqueue --library <name> [--job-type proxy|exif|ai_vision|embed] [--path <path>] [--asset <id>] [--since <iso>] [--until <iso>] [--missing-proxy] [--missing-thumbnail] [--force] [--retry-failed]` — Enqueue processing jobs for a library. Short form: `-l <name>`. `--retry-failed` re-enqueues only assets with failed jobs (mutually exclusive with `--force`). `embed` enqueues assets that have a proxy but no embeddings yet.
- `lumiverb search --library <name> <query> [--output table|json|text] [--limit N] [--offset N]` — Search assets in a library by natural language query. Default output: Rich table. `--limit 0` fetches all results (paginated). Short form: `-l <name>`, `-o` for output.
- `lumiverb similar --library <name> <asset_id> [--limit N] [--offset N] [--output table|json|text]` — Find visually similar assets by vector similarity. Default limit 10. Short form: `-l <name>`, `-o` for output.
- Worker commands (proxy, exif, vision, embed, video-preview, video-index, video-vision, search-sync) accept `[--output human|jsonl]`. Default `human` shows Rich progress and log lines; `jsonl` streams one JSON event per line (event, stage, and optional metrics) for consumption by the pipeline supervisor or other tooling.
- Worker commands that touch artifacts (proxy, vision, embed, video-preview, video-index, video-vision) accept `[--remote-storage]`. When set (or when `LUMIVERB_ARTIFACT_STORE=remote` is in the environment), workers use the HTTP artifact upload/download API instead of reading/writing `DATA_DIR` directly. Default is local mode (no flag, `LUMIVERB_ARTIFACT_STORE=local` or unset). This is the primary mechanism for running workers on a client machine that has no access to the server's data directory.
- `worker vision` and `worker embed` no longer require direct access to `DATA_DIR` — they read proxy bytes via the artifact store abstraction (local: reads from `DATA_DIR`; remote: downloads via `GET /v1/assets/{id}/artifacts/proxy`). They write bytes to a temp file, pass the path to the provider, then clean up.
- `lumiverb worker embed [--library <name>] [--once] [--output human|jsonl]` — Run the embedding worker (CLIP vectors for similarity search). Use `-l <name>` to scope to one library; `--once` processes the queue until empty then exits.
- `lumiverb worker search-sync --library <name> [--once] [--path <subpath>] [--force-resync] [--output human|jsonl]` — Run the search sync worker. Drains search_sync_queue, indexes asset metadata to Quickwit (if enabled), falls back gracefully if Quickwit is unavailable. `--path` scopes sync to a subfolder. `--force-resync` re-enqueues all assets regardless of prior sync status. Shows progress and summary table on completion (or JSONL when `--output jsonl`).
- `lumiverb pipeline run [--library <name>] [--media-type image|video|all] [--path <subpath>] [--once] [--skip-scan] [--force] [--interval N] [--lock-timeout M] [--log-file <path>]` — Run the pipeline supervisor with a live dashboard. Acquires the pipeline lock, optionally runs a scan, then polls status and runs workers until queues are empty (in `--once` mode) or continuously. When `--log-file` is provided, all pipeline output is appended to that file; otherwise it defaults to a timestamped file under `/tmp/pipeline.<utc_ms>.log` and the chosen path is shown in the dashboard.

Shell alias (one-shot sync): `function lumi-search-sync() { lumiverb worker search-sync --library "$1" --once; }`

Output: Rich tables for list; green success for create; errors handled by client (stderr + exit 1).

## Soft-delete and the active_assets view

The CLI is an API client and never queries the DB directly, so it is not subject to the soft-delete rules below. However, any CLI code that interprets asset data from API responses must treat missing assets (404) as trashed — do not assume a 404 is an error.

The API server enforces these rules (see `docs/cursor-api.md` for the full contract):
- All asset reads go through the `active_assets` view (`deleted_at IS NULL`).
- Scanning a file that was previously trashed **restores** it (same `asset_id`, `deleted_at` cleared). It does not create a new record and does not leave a zombie (updated but still trashed).
- `search_sync_queue.pending_count()` counts both `pending` and expired-`processing` rows to match what `claim_batch()` will actually process. Showing only `pending` produces a misleading progress total when rows are stuck after an interrupted sync run.
