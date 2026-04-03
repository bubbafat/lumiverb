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
- `src/cli/scan.py` — Scan phase (ADR-011): discover files, SHA comparison, EXIF extraction, proxy generation, upload, proxy cache with SHA sidecar

Entry point: `lumiverb = "src.cli:main"` (setuptools); `main()` invokes the Typer app.

## Commands

### Core Operations

#### Scan
- `lumiverb scan --library <name> [--path-prefix <subdir>] [--force] [--concurrency N] [--media-type image|video|all] [--dry-run]` — Discover files, compute SHA-256, extract EXIF, generate 2048px proxy, upload to server, cache proxy locally. Scan is the only operation that touches source files. Change detection compares source file SHA-256 against server-stored values: new files get full scan, changed files get re-scanned (same asset_id, enrichment flags reset), unchanged files are skipped (proxy cache populated from server if missing), deleted files are soft-deleted. `--force` re-scans unchanged files. `--path-prefix` scopes both scanning and deletion detection to a subdirectory. `--dry-run` shows the scan summary without making changes.

#### Enrich
- `lumiverb enrich [--library <name>] [--job-type embed|vision|faces|redetect-faces|ocr|video-scenes|scene-vision|search-sync|all] [--dry-run] [--concurrency N] [--force]` — Run enrichment on assets with missing pipeline outputs. Reads proxies from the local cache (populated by scan) and runs inference: CLIP embeddings, vision AI, OCR, face detection, search sync. On cache miss, downloads the proxy from the server. `redetect-faces` re-runs face detection on ALL images with quality gates. Omit `--library` to enrich all libraries.

#### Search
- `lumiverb search --library <name> --query <query> [--output table|json|text] [--media-type all|image|video] [--limit N] [--offset N]` — Search assets in a library by natural language query. `--limit 0` fetches all results (paginated).

#### Similar
- `lumiverb similar --library <name> [--asset-id <id> | --path <rel_path> | --image <file>] [--limit N] [--offset N] [--output table|json|text] [--from-ts N] [--to-ts N] [--asset-types image,video] [--camera-make X] [--camera-model X]` — Find visually similar assets by vector similarity. Supply one of: `--asset-id` (existing asset), `--path` (relative path in library), or `--image` (local image file).

#### Download
- `lumiverb download --library <name> --asset-id <id> [--path <rel_path>] [--size proxy|thumbnail] [--output <file>]` — Download proxy or thumbnail for an asset.

### Management

#### Config
- `lumiverb config set [--api-url <url>] [--api-key <key>] [--admin-key <key>] [--vision-api-url <url>] [--vision-api-key <key>] [--vision-model-id <id>]` — Write config.
- `lumiverb config show` — Show current config.

#### Library
- `lumiverb library create <name> <path>` — Create a library.
- `lumiverb library list` — List libraries.
- `lumiverb library update <name> [--name <new>] [--root-path <path>]` — Update library.
- `lumiverb library delete <name>` — Soft delete (trash).
- `lumiverb library empty-trash` — Permanently delete trashed libraries.

#### Collection
- `lumiverb collection list [--json]` — List collections.
- `lumiverb collection create --name <name> [--description <desc>] [--visibility private|shared|public]` — Create collection.
- `lumiverb collection show --id <col_id> [--json]` — Show collection details.
- `lumiverb collection add --id <col_id> --asset-id <id> [...]` — Add assets.
- `lumiverb collection remove --id <col_id> --asset-id <id> [...]` — Remove assets.
- `lumiverb collection delete --id <col_id>` — Delete collection.

#### User
- `lumiverb user create --email <email> [--role admin|editor|viewer]` — Create user (prompts for password).
- `lumiverb user list` — List all users.
- `lumiverb user set-role --email <email> --role <role>` — Change user role.
- `lumiverb user remove --email <email>` — Remove user.

#### Keys
- `lumiverb keys list` — List API keys for current tenant.
- `lumiverb keys create [--label <label>] [--role admin|editor|viewer]` — Create API key.
- `lumiverb keys revoke <key_id>` — Revoke an API key.

#### Filter
- `lumiverb filter list [--library <name>]` — List path filters.
- `lumiverb filter add <pattern> --include|--exclude [--library <name>]` — Add filter.
- `lumiverb filter remove <filter_id> [--library <name>]` — Remove filter.

### Admin / Ops

#### Admin
- `lumiverb admin maintenance [--start] [--end] [--message "..."]` — Maintenance mode control.
- `lumiverb admin keys create --tenant-id <id> --name <label> [--admin-key <key>]` — Create API key for tenant.
- `lumiverb admin keys list --tenant-id <id> [--admin-key <key>]` — List API keys for tenant.
- `lumiverb admin tenants list [--admin-key <key>]` — List all tenants.
- `lumiverb admin tenants set-vision --tenant-id <id> [--vision-api-url <url>] [--vision-api-key <key>] [--vision-model-id <id>]` — Set vision config for tenant.
- `lumiverb admin vision-test --path <dir> [--url <url>] [--api-key <key>]` — Test vision API against images.

#### Maintenance
- `lumiverb maintenance cleanup [--library <name>] [--execute]` — Remove orphaned files (dry-run by default).
- `lumiverb maintenance search-sync [--library <name>] [--force]` — Push stale assets to search index.
- `lumiverb maintenance cleanup-dismissed` — Delete dismissed people with zero face matches.
- `lumiverb maintenance upgrade [--dry-run] [--max-steps N] [--step <step_id>] [--force]` — Run tenant-level upgrade steps idempotently.

Output: Rich tables for list; green success for create; errors handled by client (stderr + exit 1).

## Soft-delete and the active_assets view

The CLI is an API client and never queries the DB directly, so it is not subject to the soft-delete rules below. However, any CLI code that interprets asset data from API responses must treat missing assets (404) as trashed — do not assume a 404 is an error.

The API server enforces these rules (see `docs/cursor-api.md` for the full contract):
- All asset reads go through the `active_assets` view (`deleted_at IS NULL`).
- Ingesting a file that was previously trashed **restores** it (same `asset_id`, `deleted_at` cleared). It does not create a new record and does not leave a zombie.
