# Lumiverb CLI — Cursor Context
*Feed this to Cursor when working on the CLI.*

## Purpose
The CLI is a local agent that runs on a machine that can read the source files: the machine that holds them, or the brain through a read-only mount ([ADR-016](adr/016-operating-model.md)).
It never touches the tenant DB, Quickwit, or object storage directly — it is an API client only.

See docs/architecture.md for the full design.

## Package layout
- `src/cli/main.py` — Typer app entry point; command groups: `config`, `library`, `tenant`, `filter`, `keys`, `users`, `maintenance`, `admin`
- `src/cli/commands/` — Subcommand modules: `projects.py`, `keys.py`, `users.py`, `maintenance.py`
- `src/cli/config.py` — Local config in `~/.lumiverb/config.json` (`api_url`, `api_key`, `admin_key`, `vision_api_url`, `vision_api_key`, `vision_model_id`): `load_config`, `save_config`, `get_api_url`, `get_api_key`, `get_admin_key`
- `src/cli/client.py` — `LumiverbClient`: thin httpx wrapper with persistent connection pool, reads config for base URL and `Authorization: Bearer <api_key>`; accepts `api_key_override` for admin commands; on non-2xx prints error envelope and raises `LumiverbAPIError`
- `src/cli/ingest.py` — Per-asset ingest pipeline: discover files, generate proxies, call vision AI, upload atomically
- `src/cli/scan.py` — Scan phase (ADR-011): discover files, SHA comparison, EXIF extraction, proxy generation, upload, proxy cache with SHA sidecar

Entry point: `lumiverb = "src.client.cli:main"` (setuptools); `main()` invokes the Typer app.

## Commands

### Core Operations

#### Scan
- `lumiverb scan --library <name> [--path-prefix <subdir>] [--force] [--thorough] [--concurrency N] [--media-type image|video|all] [--dry-run] [--allow-moves] [--skip-moves] [--allow-mass-delete]` — Discover files, compute SHA-256, extract EXIF, generate 2048px proxy, upload to server, cache proxy locally. Scan is the only operation that touches source files. **Fast mode (default)**: files whose mtime and size match the server are skipped without hashing. `--thorough` forces SHA-256 comparison on all existing files. `--force` re-scans everything regardless of SHA. Change detection compares source file SHA-256 against server-stored values: new files get full scan, changed files get re-scanned (same asset_id, enrichment flags reset), unchanged files are skipped (proxy cache populated from server if missing), deleted files are soft-deleted. **Move detection**: when a new local file's SHA matches a server asset whose path is no longer on disk, this is treated as a file move. `--allow-moves` automatically updates paths on the server. `--skip-moves` ignores moves (doesn't treat as new or deleted). Without either flag, an interactive prompt offers: perform moves, skip, or abort. No destructive actions occur before the move decision. `--dry-run` with moves reports them and suggests `--allow-moves`. `--allow-moves` and `--skip-moves` are mutually exclusive. `--path-prefix` scopes scanning and deletion detection to a subdirectory. **Safety (ADR-016)**: `rel_path` is stored in Unicode NFC (as the macOS app does) and source files are found on disk whatever normalization their names use. Files the user trashed are skipped (`GET /v1/libraries/{id}/ignored-paths`), and files no longer on disk are soft-deleted with reason `missing`, so they come back if they reappear. If more than 50 files and more than 5% of the library would be deleted, the scan skips deletions as a likely mount problem; `--allow-mass-delete` applies them.

#### Enrich
- `lumiverb enrich [--library <name>] [--job-type probe|embed|vision|faces|redetect-faces|ocr|transcribe|video-scenes|scene-vision|search-sync|all] [--dry-run] [--concurrency N] [--force]` — Run enrichment on assets with missing pipeline outputs. `probe` runs ffprobe on source videos that have no facet yet (frame rate, timecode, audio layout, display size, duration) and runs first in `all`, since its duration makes videos eligible for transcription and scenes; scan already probes new videos. Reads proxies from the local cache (populated by scan) and runs inference: CLIP embeddings, vision AI, OCR, face detection, video transcription, search sync. On cache miss, downloads the proxy from the server. `redetect-faces` re-runs face detection on ALL images with quality gates. `transcribe` uses faster-whisper to transcribe video audio (requires source files, not proxy cache). Omit `--library` to enrich all libraries.

#### Search
- `lumiverb search --library <name> --query <query> [--output table|json|text] [--media-type all|image|video] [--limit N]` — Search assets in a library by natural language query via `GET /v1/query` (`f=library:`, `f=query:`, `f=media:`), ranked by relevance. `--limit 0` fetches all results (cursor-paginated). The table shows the match snippet, and the time range for scene hits.

#### Similar
- `lumiverb similar --library <name> [--asset-id <id> | --path <rel_path> | --image <file>] [--limit N] [--offset N] [--output table|json|text] [--from-ts N] [--to-ts N] [--asset-types image,video] [--camera-make X] [--camera-model X]` — Find visually similar assets by vector similarity. Supply one of: `--asset-id` (existing asset), `--path` (relative path in library), or `--image` (local image file).

#### Download
- `lumiverb download --library <name> --asset-id <id> [--path <rel_path>] [--size proxy|thumbnail] [--output <file>]` — Download proxy or thumbnail for an asset.

### Management

#### Config
- `lumiverb config set [--api-url <url>] [--api-key <key>] [--admin-key <key>] [--vision-api-url <url>] [--vision-api-key <key>] [--vision-model-id <id>]` — Write config.
- `lumiverb config show` — Show current config.

#### Library
- `lumiverb library create --name <name> --path <path>` — Create a library.
- `lumiverb library list` — List libraries.
- `lumiverb library update <name> [--name <new>] [--root-path <path>]` — Update library.
- `lumiverb library delete <name>` — Soft delete (trash).
- `lumiverb library empty-trash` — Permanently delete trashed libraries.

#### Project
- `lumiverb project list [--json] [--archived | --all]` — List projects (active unless `--archived` or `--all`).
- `lumiverb project create --name <name> [--description <desc>] [--visibility private|shared|public]` — Create project.
- `lumiverb project show --id <project_id> [--json]` — Show project details.
- `lumiverb project add --id <project_id> --asset-id <id> [...]` — Add assets.
- `lumiverb project remove --id <project_id> --asset-id <id> [...]` — Remove assets.
- `lumiverb project delete --id <project_id>` — Delete project.
- `lumiverb project archive --id <project_id>` — Archive: it leaves the sidebar and pickers but keeps its clips.
- `lumiverb project restore --id <project_id>` — Restore an archived project.
- `lumiverb project export --id <project_id> --format fcp7|fcpxml [--prefix <path>] [--output <file>]` — Export a bin of master clips for DaVinci Resolve / Premiere Pro (`fcp7`) or Final Cut Pro (`fcpxml`). Writes `<project name>.xml|.fcpxml` unless `--output` is given; `--prefix` points clips at another location of the originals (e.g. a travel SSD). Reports what was left out or approximated: photos (video only for now), videos with no known length, and unprobed videos exported at the fallback rate.

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
- `lumiverb maintenance cleanup [--library <name>] [--execute]` — Remove orphaned files (dry-run by default). Needs an admin API key.
- `lumiverb maintenance search-sync [--library <name>] [--force]` — Push stale assets to search index.
- `lumiverb maintenance cleanup-dismissed` — Delete dismissed people with zero face matches.
- `lumiverb maintenance upgrade [--dry-run] [--max-steps N] [--step <step_id>] [--force]` — Run tenant-level upgrade steps idempotently.

Output: Rich tables for list; green success for create; errors handled by client (stderr + exit 1).

## Soft-delete and the active_assets view

The CLI is an API client and never queries the DB directly, so it is not subject to the soft-delete rules below. However, any CLI code that interprets asset data from API responses must treat missing assets (404) as trashed — do not assume a 404 is an error.

The API server enforces these rules (see `docs/cursor-api.md` for the full contract):
- All asset reads go through the `active_assets` view (`deleted_at IS NULL`).
- Ingesting a file the scanner marked missing (`deleted_reason` `missing` or unset) **restores** it (same `asset_id`, `deleted_at` cleared). It does not create a new record and does not leave a zombie.
- A file the user trashed (`deleted_reason = "user"`), or emptied from the trash, is **not** restored: ingest returns 409 and the scan skips it via `GET /v1/libraries/{id}/ignored-paths` (ADR-016).
