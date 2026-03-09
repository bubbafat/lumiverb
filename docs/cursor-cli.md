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

Entry point: `lumiverb = "src.cli:main"` (setuptools); `main()` invokes the Typer app.

## Commands
- `lumiverb config set --api-url <url> --api-key <key>` — write config
- `lumiverb config show` — show api_url and whether api_key is set
- `lumiverb library create <name> <path>` — POST /v1/libraries
- `lumiverb library list` — GET /v1/libraries (Rich table: ID, Name, Root path, Scan status, Vision Model, Last scan; trashed libraries hidden)
- `lumiverb library set-model <library_id> <model>` — PATCH vision_model_id. Use `moondream` for local Moondream; any other string for OpenAI-compatible API (via VISION_API_URL).
- `lumiverb library delete <name>` — Soft delete: move library to trash (prompt for confirmation)
- `lumiverb library empty-trash` — Permanently delete all trashed libraries and their assets (prompt for confirmation)
- `lumiverb scan --library <name> [--path <subpath>] [--force]` — Scan a library for media files; discovers/upserts assets via API, reports added/updated/skipped/missing.

Output: Rich tables for list; green success for create; errors handled by client (stderr + exit 1).
