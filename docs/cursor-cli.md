# Lumiverb CLI — Cursor Context
*Feed this to Cursor when working on the CLI.*

## Purpose
The CLI is a local agent that runs on the machine where source files live.
It never touches the tenant DB, Quickwit, or object storage directly — it is an API client only.

See docs/architecture.md for the full design.

## Package layout
- `src/cli/main.py` — Typer app entry point; command groups: `config`, `library`, `tenant`, `filter`, `keys`, `users`, `maintenance`, `admin`
- `src/cli/config.py` — Local config in `~/.lumiverb/config.json` (`api_url`, `api_key`, `admin_key`, `vision_api_url`, `vision_api_key`): `load_config`, `save_config`, `get_api_url`, `get_api_key`, `get_admin_key`
- `src/cli/client.py` — `LumiverbClient`: thin httpx wrapper, reads config for base URL and `Authorization: Bearer <api_key>`; accepts `api_key_override` for admin commands; on non-2xx prints error envelope and exits 1
- `src/cli/progress.py` — `UnifiedProgress`: unified layout (spinner + bar + N/M units + counters) for all long-running commands (scan, workers, search-sync). Disabled when not a terminal.

Entry point: `lumiverb = "src.cli:main"` (setuptools); `main()` invokes the Typer app.

## Commands
- `lumiverb config set [--api-url <url>] [--api-key <key>] [--admin-key <key>] [--vision-api-url <url>] [--vision-api-key <key>]` — write config. Vision API settings override tenant defaults (hybrid: client config wins if set, tenant config is the fallback).
- `lumiverb config show` — show api_url, whether api_key is set, whether admin_key is set, vision_api_url, vision_api_key
- `lumiverb library create <name> <path>` — POST /v1/libraries
- `lumiverb library list` — GET /v1/libraries (Rich table: ID, Name, Root path, Scan status, Vision Model, Last scan; trashed libraries hidden)
- `lumiverb library set-model --library <name> --model <model_id>` — PATCH vision_model_id. Model ID is any OpenAI-compatible model name (e.g. `qwen3-visioncaption-2b`). Vision API URL and key are set at the tenant level.
- `lumiverb library delete <name>` — Soft delete: move library to trash (prompt for confirmation)
- `lumiverb library empty-trash` — Permanently delete all trashed libraries and their assets (prompt for confirmation)
- `lumiverb tenant list [--admin-key <key>]` — List all tenants (tenant_id, name, plan, status). Admin key falls back to saved config.
- `lumiverb tenant set-vision --tenant-id <id> [--vision-api-url <url>] [--vision-api-key <key>] [--admin-key <key>]` — Set the OpenAI-compatible vision API URL and/or key for a tenant. Stored on the tenant record; passed to workers automatically via job payload. Admin key falls back to saved config.
- `lumiverb ingest --library <name> [--path <subpath>] [--force] [--concurrency N] [--skip-vision] [--media-type image|video|all]` — Scan and ingest a library in one pass. Images: proxy + EXIF + vision AI → atomic upload. Videos (stage 1): poster frame + EXIF + 10-sec preview → atomic upload. Processing order: all images first, then videos. `--skip-vision` skips AI; without it, vision must be configured or the command fails with setup instructions. `--media-type` (default `all`) filters to just images or videos. Client sends WebP proxy to minimize server CPU.
- `lumiverb repair --library <name> [--path <subpath>] [--concurrency N]` — Re-process assets that are missing metadata (vision, EXIF). Downloads proxy from server, runs processing client-side, posts results back.
- `lumiverb filter list [--library <name>]` — List path filters. Without `--library`, shows tenant defaults. With `--library`, shows library-specific filters.
- `lumiverb filter add <pattern> --include|--exclude [--library <name>]` — Add a path filter. Without `--library`, adds as tenant default (applies to all libraries). With `--library`, adds to that library only.
- `lumiverb filter remove <filter_id> [--library <name>]` — Remove a filter by ID. IDs prefixed `tpfd_` for tenant defaults, `lpf_` for library filters.
- `lumiverb admin maintenance` — Show current maintenance mode status (`active`, `message`, `started_at`).
- `lumiverb admin maintenance --start [--message "..."]` — Enable maintenance mode; workers stop claiming jobs immediately.
- `lumiverb admin maintenance --end` — Disable maintenance mode; workers resume normally.
- `lumiverb upgrade [--dry-run] [--max-steps N] [--step <step_id>] [--force]` — Run tenant-level upgrade steps (schema/backfills) idempotently. **Requires maintenance mode to be active** (enforced at runtime; `--dry-run` bypasses this check). With `--dry-run`, lists pending steps without executing. With `--step`, run only a single step; by default it refuses to run if preceding steps are not complete unless `--force` is provided (with a confirmation prompt).
- `lumiverb search --library <name> <query> [--output table|json|text] [--limit N] [--offset N]` — Search assets in a library by natural language query. Default output: Rich table. `--limit 0` fetches all results (paginated). Short form: `-l <name>`, `-o` for output.
- `lumiverb similar --library <name> <asset_id> [--limit N] [--offset N] [--output table|json|text]` — Find visually similar assets by vector similarity. Default limit 10. Short form: `-l <name>`, `-o` for output.
Output: Rich tables for list; green success for create; errors handled by client (stderr + exit 1).

## Soft-delete and the active_assets view

The CLI is an API client and never queries the DB directly, so it is not subject to the soft-delete rules below. However, any CLI code that interprets asset data from API responses must treat missing assets (404) as trashed — do not assume a 404 is an error.

The API server enforces these rules (see `docs/cursor-api.md` for the full contract):
- All asset reads go through the `active_assets` view (`deleted_at IS NULL`).
- Ingesting a file that was previously trashed **restores** it (same `asset_id`, `deleted_at` cleared). It does not create a new record and does not leave a zombie.
