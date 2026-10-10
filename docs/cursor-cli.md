# Lumiverb CLI — Cursor Context
*Feed this to Cursor when working on the CLI.*

## Purpose
The CLI is a local agent that runs on a machine that can read the source files: the machine that holds them, or the brain through a read-only mount ([ADR-016](adr/016-operating-model.md)).
It never touches the tenant DB, Quickwit, or object storage directly — it is an API client only.

See docs/architecture.md for the full design.

## Package layout
- `src/client/cli/main.py` — Typer app entry point; top-level commands `pause`, `resume`, `download`, `scan`, `enrich`, `search`, `similar`; command groups `config`, `library`, `project`, `keys`, `user`, `settings`, `filter`, `maintenance`, `archive`, `trash`, `producers`, `admin` (with `admin keys`, `admin tenants`). `config`, `library`, `filter` and `admin` are defined in `main.py`.
- `src/client/cli/commands/` — Subcommand modules: `archive.py`, `keys.py`, `maintenance.py`, `pausing.py`, `producers.py`, `projects.py`, `settings.py`, `trash.py`, `users.py`
- `src/client/cli/config.py` — Local config in `~/.lumiverb/config.json`, written atomically (a temp file renamed over it) with mode 0600, since it holds keys. A config file that can't be read or parsed is an error (`ConfigError`: the CLI names the file and exits 1), never read as the defaults. Only how this machine works (`api_url`, `api_key`, `admin_key`, `root_map`, concurrency and batch sizes, `analysis_proxy_encoder`, `analysis_proxy_decoder`, `gpu_decodes`, `analysis_cache_gb`, `cache_home`): `load_config`, `save_config`, `get_api_url`, `get_api_key`, `get_admin_key`. What changes an artifact's output lives only on the server (one source of truth): the producers' settings (`GET /v1/producers`, read once per run by `src/client/cli/producer_settings.py`: the Whisper model, the analysis proxy's size and quality, the vision prompts) and the AI machines and each job's model (Settings → AI; `/v1/ai`), each machine with its own limit of requests at once. Keys an older config still has (`vision_*` including `vision_concurrency`, `ocr_concurrency`, `whisper_model`, `proxy_max_edge`, `analysis_proxy_max_edge`) are ignored. The encoder and decoder are this machine's and aren't tracked: `analysis_proxy_decoder` is `auto` (decode originals on the GPU through Vulkan when this ffmpeg can open a device; ffmpeg decodes on the CPU what the GPU can't, and a render whose GPU decoding fails runs again on the CPU), `cpu`, or an ffmpeg hwaccel such as `cuda`. Decoding is exact, so the proxy is the same either way. `gpu_decodes` (default 1; 0 = never) says how many renders decode on the GPU at once, and each only while the first NVIDIA GPU has at least 1.5 GB free beside the models using it; the rest decode on the CPU meanwhile. Before vision work, `src/client/cli/vision_guard.py` checks each machine doing vision (`src/client/cli/ai_pool.py`) and tells the server what each said; requests go to the online ones, each up to its limit, and one that fails is skipped and checked again a minute later. With no machine left, vision steps wait and no clip is charged. Each step reports the clips it couldn't make (`src/client/cli/failure_report.py`, `POST /v1/producers/failures`).
- `src/processing/roots.py` — Where a library's root is on this machine: the server keeps the editing machine's path, the CLI's `root_map` (or the scheduler's `LUMIVERB_ROOT_MAP`) maps prefixes here (`reachable_root` gives up on a hung mount after a timeout)
- `src/processing/video/analysis_proxy.py`, `src/processing/proxy/analysis_cache.py` — Render analysis proxies with ffmpeg; local cache of them (downloads on first use). The scheduler renders them; the CLI doesn't.
- `src/client/cli/client.py` — `LumiverbClient`: thin httpx wrapper with persistent connection pool, reads config for base URL and `Authorization: Bearer <api_key>`; accepts `api_key_override` for admin commands; on non-2xx prints error envelope and raises `LumiverbAPIError`
- `src/processing/` — What the CLI's scan and the scheduler share: the API client, this machine's settings (`machine.py`), scanning (`scan.py`, `ingest.py`), and the media tools. The CLI's own client (`src/client/cli/client.py`) fills in the URL and key from its config.
- `src/client/cli/commands/enrich.py` — `lumiverb enrich`: asks the scheduler for work now (`POST /v1/producers/run`). There is no `repair` command.
- `src/processing/scan.py` — Scan phase (ADR-011): discover files, SHA comparison, EXIF extraction, proxy generation, upload, proxy cache with SHA sidecar

Entry point: `lumiverb = "src.client.cli:main"` (setuptools); `main()` invokes the Typer app.

## Commands

### Core Operations

#### Scan
- `lumiverb scan --library <name> [--path-prefix <subdir>] [--force] [--thorough] [--concurrency N] [--media-type image|video|all] [--dry-run] [--allow-moves] [--skip-moves] [--allow-mass-delete]` — Discover files, compute SHA-256, extract EXIF (GPS of (0, 0) is no fix and isn't kept; the time offset and GPS accuracy are kept, ADR-017), generate 2048px proxy, upload to server, cache proxy locally. Scan is the only operation that touches source files. **Fast mode (default)**: files whose mtime and size match the server are skipped without hashing. `--thorough` forces SHA-256 comparison on all existing files. `--force` re-scans everything regardless of SHA. Change detection compares source file SHA-256 against server-stored values: new files get full scan, a changed file is a new clip (Robert, Oct 9: nothing carries over), and the old one is archived as missing where it was, back if its content is, unchanged files are skipped (proxy cache populated from server if missing), deleted files are soft-deleted. **Move detection**: when a new local file's SHA matches a server asset whose path is no longer on disk, this is treated as a file move. `--allow-moves` automatically updates paths on the server. `--skip-moves` ignores moves (doesn't treat as new or deleted). Without either flag, an interactive prompt offers: perform moves, skip, or abort. No destructive actions occur before the move decision. `--dry-run` with moves reports them and suggests `--allow-moves`. `--allow-moves` and `--skip-moves` are mutually exclusive. When the account doesn't follow moves (`settings follow-moves off`), scan looks for none: the old path is archived, the new one is a new asset, and `--skip-moves` doesn't hold back deletions. When the setting can't be read, scan looks for no moves either (a server that follows moves still restores the asset at the new path by content), and `--skip-moves` still holds back deletions. Assets the server moved to an empty copy of their file (copy, then delete) count as moved, not deleted. `--path-prefix` scopes scanning and deletion detection to a subdirectory. **Safety (ADR-016)**: `rel_path` is stored in Unicode NFC (as the macOS app does) and source files are found on disk whatever normalization their names use. Files a person trashed or archived are skipped (`GET /v1/libraries/{id}/ignored-paths`), and files no longer on disk are soft-deleted with reason `missing`, so they come back if they reappear. The missing clips go to the server in one request; when more than 50 clips and more than half of a library's clips in sight would be archived as missing, the server asks first (409 `mass_missing`) and the scan skips them as a likely mount problem (Robert, Oct 9); `--allow-mass-delete` answers with the count and applies them. Files modified in the last 30 seconds may still be copying: they wait for a later scan and are never taken for deleted. The library root is resolved through `config map-root`; when it can't be read, scan says so and exits 1. A root `/etc/fstab` lists as a mount counts only while it's mounted, and an empty root never counts. When files look missing (or moved), the root is checked again before anything is archived or moved: if the storage went away during the scan (a share unmounted mid-walk), nothing is archived or moved, and scan says so and exits 1.

#### Enrich
- `lumiverb enrich PRODUCER|all (--library <name> | --all) [--redo]` — Ask the scheduler on the brain to do a producer's work now (`POST /v1/producers/run`): its failing clips in that library (or every library) are tried again at once and the scheduler looks again; with `--redo` (admins) what it has made there is made again after anything missing, as new settings would (`lumiverb enrich faces --library X --redo` re-detects faces; faces people named, or said aren't a certain person, are kept). The producer and the libraries are always named: without them it does nothing and says how. It goes as the scheduler goes (tiers, pools, pauses, back-off) and says per producer what's left to make: e.g. `Faces: 3 to make, 120 to make again (redo stopped)`. Search sync is `lumiverb maintenance search-sync`.

#### Processing on the brain
There is no worker command. Processing runs in the server-side scheduler (`lumiverb-scheduler.service`, `python -m src.server.scheduler`), which ranks every job in every library; see docs/brain-setup.md. `pause`, `resume` and `producers` control and report on it.

#### Search
- `lumiverb search --library <name> --query <query> [--output table|json|text] [--media-type all|image|video] [--limit N]` — Search assets in a library by natural language query via `GET /v1/query` (`f=library:`, `f=query:`, `f=media:`), ranked by relevance. `--limit 0` fetches all results (cursor-paginated). The table shows the match snippet, and the time range for scene hits.

#### Similar
- `lumiverb similar --library <name> [--asset-id <id> | --path <rel_path> | --image <file>] [--limit N] [--offset N] [--output table|json|text] [--from-ts N] [--to-ts N] [--asset-types image,video] [--camera-make X] [--camera-model X]` — Find visually similar assets by vector similarity. Supply one of: `--asset-id` (existing asset), `--path` (relative path in library), or `--image` (local image file).

#### Download
- `lumiverb download --library <name> --asset-id <id> [--path <rel_path>] [--size proxy|thumbnail] [--output <file>]` — Download proxy or thumbnail for an asset.

### Management

#### Config
- `lumiverb config set [--api-url <url>] [--api-key <key>] [--admin-key <key>] [--cache-home <dir>]` — Write config. `admin keys` and `admin tenants` take the admin key from `--admin-key`, else `LUMIVERB_ADMIN_KEY`, else the config's `admin_key`. The vision endpoint and model are set in the web app (Settings → AI).
- `lumiverb config show` — Show current config, including root mappings.
- `lumiverb config map-root <server-prefix> <local-prefix>` — Where a library root prefix is on this machine, e.g. `/Volumes/media-01 /mnt/media-01`. Libraries keep the editing machine's path (exports point editors there); the CLI's scan and `library list` use the mapped one (the scheduler has its own map: `LUMIVERB_ROOT_MAP`, docs/brain-setup.md). Whole folders, longest prefix wins, Unicode form and trailing slashes ignored. Saves even when the target isn't mounted, with a warning.
- `lumiverb config unmap-root <server-prefix>` — Remove a mapping.
- `lumiverb settings show` — Account-wide settings, e.g. `Video playback: whole video`, `On public pages: first 10 seconds`, `Follow moves and renames: on` and `Trash: deleted for good after 30 days`.
- `lumiverb settings video-preview full|<seconds>` — How much of each video plays for signed-in people (admins only; the web's Settings → Playback). `full` is the default; a number caps playback at that many seconds (1 to 86,400).
- `lumiverb settings public-preview full|<seconds>` — The same for public library and project pages: 10 seconds until set, never more than signed-in people get.
- `lumiverb settings trash-days <days>|off [--yes]` — How long clips, libraries and projects stay in the trash before they're deleted for good (admins only; the web's Settings → Files): 30 until changed, 1–3,650, or `off` to empty it by hand only. Fewer days says how many things would go at once and asks (`--yes` agrees). Archived clips aren't deleted on their own; they go only with their library.
- `lumiverb settings follow-moves on|off` — Whether the same content is the same asset (admins only; the web's Settings → Files). On, the default: a moved or renamed file keeps its notes, ratings and projects, and so does the copy left when the original is deleted. Off: a file at a new path is a new asset, and scans look for no moves.
- `lumiverb pause <name>` — Admins. Pause one processing switch: a producer the scheduler makes (`vision`, `ocr`, ...: nothing more of it starts), `scans` (no new or changed files found, no thumbnails or video previews made) or `upkeep` (no trash purge, file cleanup or face names spread); `all` pauses every switch. What's running finishes. Every switch works whatever the others are. Without a name it does nothing and lists the names. After acting it says the account's state: Running (none paused), Partly paused (naming what's paused) or Paused (all).
- `lumiverb resume <name>` — Admins. Resume one switch, or `all` for every one. Without a name it does nothing and lists the names. It says the state after.
- `lumiverb producers [--library NAME | --project NAME]` — What each producer has made (the web's Settings → Processing): current, missing, stale (made with another producer, model or settings than now: made again after anything missing) and failing, whether each one is paused and whether its redo is going or stopped. It starts with the account's processing state (Running, Partly paused or Paused) and whether Scans and Upkeep run. Producers are named as in the listing (`vision`, `ocr`, `clip`, `faces`, `transcript`, `analysis_proxy`, ...). Over the whole account (no `--library` or `--project`), while the scheduler runs, a Left column says about how long each producer has to go, and a line says when everything will be caught up, leaving out (and naming) paused producers, whose Left says Paused.
- `lumiverb producers redo stop <producer>` — Stop redoing a producer's stale clips (admins); what's missing is still made. Resumed by `redo resume`, or by a new model or new settings for it.
- `lumiverb producers redo resume <producer>` — Redo a producer's stale clips again (admins), after anything missing.
- `lumiverb producers settings <producer> [KEY=VALUE ...] [--yes]` — A producer's settings with what each can be; with `KEY=VALUE` (admins), change the ones its code reads (`KEY=` puts one back to its default; a value may hold `=`, as a prompt can). Text the listing cuts short is printed whole below it. New settings make what the old ones made again, after anything missing: it says how many clips and asks, unless `--yes`.
- `lumiverb producers failures [<producer>] [--library NAME] [--limit N]` — Clips whose last try failed: the error, how many tries, when the next one is, or that it was given up (after 10 tries).
- `lumiverb producers retry PRODUCER|all (--library NAME | --all | --asset ID ...)` — Try failing clips again now, given up or not (editors and admins): what's tried again is always named; without it the command does nothing and says how.

#### Library
- `lumiverb library create --name <name> --path <path>` — Create a library.
- `lumiverb library list` — List libraries; a **Here** column shows mapped roots.
- `lumiverb library report-changes <path>... [--stdin]` — Tell the brain these files or folders changed, so its scheduler scans them (`POST /v1/changes`). Paths are as this machine sees them; root mappings turn them back into the library's form. Prints how many matched a library and which didn't.
- `lumiverb library update --name <name> [--new-name <new>] [--root-path <path>]` — Update library.
- `lumiverb library delete --name <name> [--yes] [--remove-from-projects]` — Move a library to the trash with everything in it (it says how many archived clips go with it); it's deleted for good after the trash days. When its clips are in projects it names them and asks (in the trash they're hidden there; deleted for good they leave them); `--remove-from-projects` answers for scripts (`--yes` without it stops if there are any).
- `lumiverb library restore --name <name>` — Take a library out of the trash with the clips that went with it. It comes back private.
- `lumiverb library empty-trash (--name <name> | --all) [--remove-from-projects] [--yes]` — Permanently delete trashed libraries: that one, or all of them (admins only); exactly one of `--name` or `--all`. First lists the projects their clips are in (deleting removes the clips from those projects) and asks, unless `--yes`; with `--yes` and clips in projects it stops (exit 2) unless `--remove-from-projects`.
- `lumiverb archive add [<clip ids>...] | --library <name|id> --folder <path> [--yes]` — Archive clips: out of sight, kept forever with everything they have; scans leave them archived. By id, or every clip under a folder, recursively (`--folder ''` for the whole library; a folder asks first unless `--yes`); a file added to the folder later shows up as usual. Says which ids it skipped (not in sight).
- `lumiverb archive restore [<clip ids>...] | --library <name|id> --folder <path>` — Unarchive clips a person archived. A missing file's clip is skipped: it comes back when the file does.
- `lumiverb archive list [--library <name|id>] [--folder <path>] [--missing | --by-hand] [--limit N]` — Archived clips, most recent first, marking those whose file is missing.
- `lumiverb archive delete-missing (--library <name|id> [--folder <path>] | --all) [--remove-from-projects] [--yes]` — Admins: delete for good the clips whose files went missing, with everything made or written for them: one library's, or every library's (`--all`); exactly one. The server says how many first, and asks about projects that use them; `--yes` goes ahead with however many there are (and stops at projects without `--remove-from-projects`). A file that comes back afterwards is a new clip.
- `lumiverb trash add <clip ids>... [--remove-from-projects] [--yes]` — Move clips (in sight or archived) to the trash; deleted for good after the trash days. When projects use them it names them and asks.
- `lumiverb trash restore <clip ids>...` — Take clips out of the trash, back to where they were: in sight, or the archive for clips archived before (it says which). A library's clips come back with the library.
- `lumiverb trash list [--library <name|id>] [--folder <path>] [--limit N]` — Clips in the trash, most recent first, with the day each is deleted for good.
- `lumiverb trash empty (<clip ids>... | --all [--library <name|id>] [--folder <path>]) [--remove-from-projects] [--yes]` — Delete clips in the trash for good now, to free space (admins only), after a confirmation: chosen clips, or everything in the trash (of a library, under a folder). Never archived clips (trash them first), nor a trashed library's (they go with it).

#### Project
- `lumiverb project list [--json] [--archived | --all | --trashed]` — List projects (active unless `--archived`, `--all` or `--trashed`).
- `lumiverb project create --name <name> [--description <desc>] [--visibility private|shared|public]` — Create project.
- `lumiverb project show --id <project_id> [--json]` — Show project details.
- `lumiverb project add --id <project_id> --asset-id <id> [...]` — Add assets.
- `lumiverb project remove --id <project_id> --asset-id <id> [...]` — Remove assets.
- `lumiverb project delete --id <project_id>` — Move a project to the trash (no prompt: it's reversible). Prints the undo command.
- `lumiverb project archive --id <project_id>` — Archive: it leaves the sidebar and pickers but keeps its clips.
- `lumiverb project restore --id <project_id> [--with-clips | --without-clips]` — Take a project out of the trash, back to active or archived as it was; a project that isn't in the trash is un-archived. If clips in it are in the trash, asks whether to restore them too (they come back everywhere) unless a flag says.
- `lumiverb project restore-clips --id <project_id>` — Restore the project's clips that someone trashed. Clips whose files went missing come back when the files do.
- `lumiverb project empty-trash (--id <project_id> ... | --all) [--yes]` — Delete trashed projects for good (the named ones, or the whole trash with `--all`; exactly one) after a confirmation. Their clips stay in the libraries.
- `lumiverb project export --id <project_id> --format fcp7|fcpxml [--output <file>]` — Export a bin of master clips for DaVinci Resolve / Premiere Pro (`fcp7`) or Final Cut Pro (`fcpxml`). Writes `<project name>.xml|.fcpxml` unless `--output` is given. Clips point at the originals as the libraries know them (relink in the editor where its paths differ); photos are in it as stills of 5 s. Reports how many photos are stills, and what was left out or approximated: videos with no known length, clips in the trash, clips whose files are missing, clips in a deleted library, archived clips, and unprobed videos exported at the fallback rate.

#### User
- `lumiverb user create --email <email> [--role admin|editor|viewer]` — Create user (prompts for password).
- `lumiverb user list` — List all users.
- `lumiverb user set-role --email <email> --role <role>` — Change user role.
- `lumiverb user remove --email <email> [--yes]` — Remove user (asks first unless `--yes`).

#### Keys
- `lumiverb keys list` — List API keys for the current account.
- `lumiverb keys create [--label <label>] [--role admin|editor|viewer]` — Create API key.
- `lumiverb keys revoke --key-id <key_id>` — Revoke an API key.

#### Filter
- `lumiverb filter list [--library <name>]` — List path filters (the account's defaults without `--library`).
- `lumiverb filter add <pattern> --include|--exclude (--library <name> | --tenant-default)` — Add a filter to a library or to the account's defaults; exactly one.
- `lumiverb filter remove <filter_id> (--library <name> | --tenant-default)` — Remove a filter from a library or the account's defaults; exactly one.

### Admin / Ops

#### Admin
- `lumiverb admin maintenance [--start] [--end] [--message "..."]` — Maintenance mode control.
- `lumiverb admin keys create --tenant-id <id> --name <label> [--admin-key <key>]` — Create API key for tenant.
- `lumiverb admin keys list --tenant-id <id> [--admin-key <key>]` — List API keys for tenant.
- `lumiverb admin tenants list [--admin-key <key>]` — List all tenants.
- `lumiverb admin tenants set-vision --tenant-id <id> [--vision-api-url <url>] [--vision-api-key <key>] [--vision-model-id <id>] [--admin-key <key>]` — Set a tenant's vision config as the operator: the URL and key are its first AI machine doing vision (made or moved; an empty URL removes it), the model its vision model. Settings → AI checks the model is offered; this doesn't.
- `lumiverb admin vision-test --path <dir> [--url <url>] [--api-key <key>] [--model <id>] [--output <file>]` — Test vision API against images.

#### Maintenance
- `lumiverb maintenance cleanup (--library <name|id> | --all) [--execute]` — Remove orphaned files left after the trash is emptied: only that library, or the whole account; exactly one of `--library` or `--all`. Dry run unless `--execute`. Admins only. While the Upkeep switch is paused, `--execute` deletes nothing and says it was skipped.
- `lumiverb maintenance search-sync --all [--force]` — Push stale assets to the search index. `--all` is required (the index is the account's; there is no `--library`). `--force` clears the sync timestamps and reindexes everything. Admins only.
- `lumiverb maintenance cleanup-dismissed --all` — Delete dismissed people with zero face matches, in the caller's account only. Admins only.
- `lumiverb maintenance upgrade [--dry-run] [--max-steps N] [--step <step_id>] [--force]` — Run tenant-level upgrade steps idempotently.

Output: Rich tables for list; green success for create; errors handled by client (stderr + exit 1).

## Soft-delete and the active_assets view

The CLI is an API client and never queries the DB directly, so it is not subject to the soft-delete rules below. However, any CLI code that interprets asset data from API responses must treat missing assets (404) as trashed — do not assume a 404 is an error.

The API server enforces these rules (see `docs/cursor-api.md` for the full contract):
- All asset reads go through the `active_assets` view (`deleted_at IS NULL`).
- Ingesting a file the scanner marked missing (`deleted_reason` `missing`) **restores** it (same `asset_id`, `deleted_at` cleared). It does not create a new record and does not leave a zombie.
- A file a person trashed or archived (`deleted_reason` `"user"` or `"archived"`), or emptied from the trash, is **not** restored: ingest returns 409 and the scan skips it via `GET /v1/libraries/{id}/ignored-paths` (ADR-016).
