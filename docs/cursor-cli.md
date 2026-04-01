# Lumiverb CLI — Cursor Context
*Feed this to Cursor when working on the CLI.*

## Purpose
The CLI is a local agent that runs on the machine where source files live.
It never touches the tenant DB, Quickwit, or object storage directly — it is an API client only.

See docs/architecture.md for the full design.

## Package layout
- `src/cli/main.py` — Typer app entry point; command groups: `config`, `library`, `tenant`, `filter`, `keys`, `users`, `maintenance`, `admin`
- `src/cli/commands/` — Subcommand modules: `collections.py`, `keys.py`, `users.py`, `maintenance.py`
- `src/cli/config.py` — Local config in `~/.lumiverb/config.json` (`api_url`, `api_key`, `admin_key`, `vision_api_url`, `vision_api_key`, `vision_model_id`): `load_config`, `save_config`, `get_api_url`, `get_api_key`, `get_admin_key`
- `src/cli/client.py` — `LumiverbClient`: thin httpx wrapper with persistent connection pool, reads config for base URL and `Authorization: Bearer <api_key>`; accepts `api_key_override` for admin commands; on non-2xx prints error envelope and raises `LumiverbAPIError`
- `src/cli/ingest.py` — Per-asset ingest pipeline: discover files, generate proxies, call vision AI, upload atomically

Entry point: `lumiverb = "src.cli:main"` (setuptools); `main()` invokes the Typer app.

## Commands

### Config
- `lumiverb config set [--api-url <url>] [--api-key <key>] [--admin-key <key>] [--vision-api-url <url>] [--vision-api-key <key>] [--vision-model-id <id>]` — Write config. Vision API settings override tenant defaults (hybrid: client config wins if set, tenant config is the fallback).
- `lumiverb config show` — Show api_url, whether api_key is set, whether admin_key is set, vision_api_url, vision_api_key, vision_model_id.

### Library
- `lumiverb library create <name> <path>` — POST /v1/libraries
- `lumiverb library list` — GET /v1/libraries (Rich table: ID, Name, Root path, Scan status, Last scan; trashed libraries hidden)
- `lumiverb library delete <name>` — Soft delete: move library to trash (prompt for confirmation)
- `lumiverb library empty-trash` — Permanently delete all trashed libraries and their assets (prompt for confirmation)

### Tenant (admin)
- `lumiverb tenant list [--admin-key <key>]` — List all tenants (tenant_id, name, plan, status). Admin key falls back to saved config.
- `lumiverb tenant set-vision --tenant-id <id> [--vision-api-url <url>] [--vision-api-key <key>] [--vision-model-id <id>] [--admin-key <key>]` — Set the OpenAI-compatible vision API URL, key, and/or model ID for a tenant. Stored on the tenant record. Admin key falls back to saved config.

### Ingest
- `lumiverb ingest --library <name> [--path <subpath>] [--force] [--concurrency N] [--skip-vision] [--skip-embeddings] [--media-type image|video|all]` — Scan and ingest a library in one pass. Images: proxy + EXIF + vision AI + CLIP embeddings → atomic upload. Videos: stage 1 (poster frame + EXIF + 10-sec preview → atomic upload), then stage 2 (scene detection via pHash drift + temporal ceiling → chunk API). Processing order: all images first, then video stage 1, then video stage 2 (scene detection). `--skip-vision` skips AI captioning; `--skip-embeddings` skips CLIP vector generation. Without `--skip-vision`, vision must be configured or the command fails with setup instructions. `--media-type` (default `all`) filters to just images or videos. Client sends WebP proxy to minimize server CPU.

### Repair
- `lumiverb repair --library <name> [--job-type embed|vision|faces|video-scenes|search-sync|all] [--dry-run] [--concurrency N]` — Detect and repair missing pipeline outputs. `embed`: backfill missing CLIP embeddings. `vision`: backfill missing AI descriptions. `faces`: detect faces using InsightFace (stores bounding boxes + ArcFace embeddings). `video-scenes`: run scene detection on videos with `video_indexed=false` (requires local source access). `search-sync`: push stale assets to Quickwit. `all` (default): run all repair types. `--dry-run` shows what would be done without executing.

### Search & Similarity
- `lumiverb search --library <name> <query> [--output table|json|text] [--media-type all|image|video] [--limit N] [--offset N]` — Search assets in a library by natural language query. Default output: Rich table. `--limit 0` fetches all results (paginated). Short form: `-l <name>`, `-o` for output.
- `lumiverb similar --library <name> <asset_id> [--path <rel_path>] [--limit N] [--offset N] [--output table|json|text]` — Find visually similar assets by vector similarity. Default limit 10. Short form: `-l <name>`, `-o` for output.
- `lumiverb similar-image <image_path> --library <name> [--limit N] [--offset N] [--output table|json|text] [--from-ts N] [--to-ts N] [--asset-types image,video] [--camera-make X] [--camera-model X]` — Upload a local image and find similar assets in a library.

### Download
- `lumiverb download --library <name> --asset-id <id> [--path <rel_path>] [--size proxy|thumbnail] [--output <file>]` — Download proxy or thumbnail for an asset.

### Filters
- `lumiverb filter list [--library <name>]` — List path filters. Without `--library`, shows tenant defaults. With `--library`, shows library-specific filters.
- `lumiverb filter add <pattern> --include|--exclude [--library <name>]` — Add a path filter. Without `--library`, adds as tenant default (applies to all libraries). With `--library`, adds to that library only.
- `lumiverb filter remove <filter_id> [--library <name>]` — Remove a filter by ID. IDs prefixed `tpfd_` for tenant defaults, `lpf_` for library filters.

### Collections
- `lumiverb collection list [--json]` — List collections you own or that are shared with you. Rich table with ID, name, asset count, visibility, ownership. `--json` for raw JSON.
- `lumiverb collection create --name <name> [--description <desc>] [--visibility private|shared|public]` — Create a new collection. Default visibility: private.
- `lumiverb collection show --id <col_id> [--json]` — Show collection details and first 50 assets. `--json` includes full asset list up to 1000.
- `lumiverb collection add --id <col_id> --asset-id <id> [--asset-id <id> ...]` — Add assets to a collection. Repeat `--asset-id` for multiple.
- `lumiverb collection remove --id <col_id> --asset-id <id> [--asset-id <id> ...]` — Remove assets from a collection.
- `lumiverb collection delete --id <col_id>` — Delete a collection (prompt for confirmation). Source assets are not affected.

### Keys
- `lumiverb keys list` — List non-revoked API keys for current tenant.
- `lumiverb keys create [--label <label>] [--role admin|editor|viewer]` — Create API key. Returns plaintext once.
- `lumiverb keys revoke <key_id>` — Revoke an API key (prompt for confirmation).

### Users
- `lumiverb create-user --email <email> [--role admin|editor|viewer]` — Create a user (prompts for password). Default role: viewer.
- `lumiverb list-users` — List all users for current tenant.
- `lumiverb set-user-role --email <email> --role <role>` — Change user role. Enforces last-admin invariant.
- `lumiverb remove-user --email <email>` — Remove user (prompt for confirmation). Enforces last-admin invariant.

### Maintenance
- `lumiverb maintenance cleanup [--library <name>] [--execute]` — Remove orphaned files. Dry-run by default; pass `--execute` to actually delete.
- `lumiverb maintenance search-sync [--library <name>]` — Push stale assets to Quickwit search index.

### Admin
- `lumiverb admin maintenance` — Show current maintenance mode status (`active`, `message`, `started_at`).
- `lumiverb admin maintenance --start [--message "..."]` — Enable maintenance mode.
- `lumiverb admin maintenance --end` — Disable maintenance mode.
- `lumiverb admin keys create --tenant-id <id> --name <label> [--admin-key <key>]` — Create API key for tenant.
- `lumiverb admin keys list --tenant-id <id> [--admin-key <key>]` — List API key metadata for tenant.
- `lumiverb admin tenants list [--admin-key <key>]` — List all tenants.

### Upgrade
- `lumiverb upgrade [--dry-run] [--max-steps N] [--step <step_id>] [--force]` — Run tenant-level upgrade steps (schema/backfills) idempotently. **Requires maintenance mode to be active** (enforced at runtime; `--dry-run` bypasses this check). With `--dry-run`, lists pending steps without executing. With `--step`, run only a single step; by default it refuses to run if preceding steps are not complete unless `--force` is provided (with a confirmation prompt).

Output: Rich tables for list; green success for create; errors handled by client (stderr + exit 1).

## Soft-delete and the active_assets view

The CLI is an API client and never queries the DB directly, so it is not subject to the soft-delete rules below. However, any CLI code that interprets asset data from API responses must treat missing assets (404) as trashed — do not assume a 404 is an error.

The API server enforces these rules (see `docs/cursor-api.md` for the full contract):
- All asset reads go through the `active_assets` view (`deleted_at IS NULL`).
- Ingesting a file that was previously trashed **restores** it (same `asset_id`, `deleted_at` cleared). It does not create a new record and does not leave a zombie.
