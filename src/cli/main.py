"""Typer CLI entry point: scan, enrich, search, similar, library, user, admin."""

from pathlib import Path
from typing import Annotated

import json as _json
import logging
import sys
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from src.cli.client import LumiverbAPIError, LumiverbClient
from src.cli.config import get_admin_key, load_config, save_config
from src.cli.commands.collections import collections_app
from src.cli.commands.keys import keys_app
from src.cli.commands.maintenance import maintenance_app
from src.cli.commands.users import user_app
from src.core.io_utils import normalize_path_prefix
from src.core.logging_config import configure_logging

_log = logging.getLogger(__name__)

app = typer.Typer()
config_app = typer.Typer(help="Manage API URL and API key.")
app.add_typer(config_app, name="config")
library_app = typer.Typer(help="Create and list libraries.")
app.add_typer(library_app, name="library")
app.add_typer(collections_app, name="collection")
app.add_typer(keys_app, name="keys")
app.add_typer(user_app, name="user")
filter_app = typer.Typer(help="Manage path filters (include/exclude patterns).")
app.add_typer(filter_app, name="filter")
app.add_typer(maintenance_app, name="maintenance")

console = Console()


@app.callback()
def _main() -> None:
    """Lumiverb media asset management CLI."""
    configure_logging()


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@config_app.command("set")
def config_set(
    api_url: Annotated[str | None, typer.Option("--api-url")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key")] = None,
    admin_key: Annotated[str | None, typer.Option("--admin-key")] = None,
    vision_api_url: Annotated[str | None, typer.Option("--vision-api-url", help="Local vision API URL (overrides tenant default).")] = None,
    vision_api_key: Annotated[str | None, typer.Option("--vision-api-key", help="Local vision API key (overrides tenant default).")] = None,
    vision_model_id: Annotated[str | None, typer.Option("--vision-model-id", help="Vision model ID override (default: auto-discover from API).")] = None,
) -> None:
    """Set API URL, API key, and/or admin key in ~/.lumiverb/config.json."""
    cfg = load_config()
    if api_url is not None:
        cfg.api_url = api_url.rstrip("/")
    if api_key is not None:
        cfg.api_key = api_key
    if admin_key is not None:
        cfg.admin_key = admin_key
    if vision_api_url is not None:
        cfg.vision_api_url = vision_api_url.rstrip("/")
    if vision_api_key is not None:
        cfg.vision_api_key = vision_api_key
    if vision_model_id is not None:
        cfg.vision_model_id = vision_model_id
    save_config(cfg)
    console.print("[green]Config saved.[/green]")


@config_app.command("show")
def config_show() -> None:
    """Show current API URL and whether an API key is set."""
    cfg = load_config()
    table = Table(show_header=False)
    table.add_column("Key", style="dim")
    table.add_column("Value")
    table.add_row("api_url", cfg.api_url)
    table.add_row("api_key", escape("[set]") if cfg.api_key else escape("[not set]"))
    table.add_row("admin_key", escape("[set]") if cfg.admin_key else escape("[not set]"))
    table.add_row("vision_api_url", cfg.vision_api_url or escape("[not set — will use tenant default]"))
    table.add_row("vision_api_key", escape("[set]") if cfg.vision_api_key else escape("[not set — will use tenant default]"))
    table.add_row("vision_model_id", cfg.vision_model_id or escape("[not set — will auto-discover from API]"))
    console.print(table)


# ---------------------------------------------------------------------------
# library
# ---------------------------------------------------------------------------


@library_app.command("create")
def library_create(
    name: Annotated[str, typer.Option("--name", "-n", help="Library name.")],
    path: Annotated[str, typer.Option("--path", "-p", help="Root path on disk.")],
) -> None:
    """Create a library with the given name and root path."""
    client = LumiverbClient()
    resp = client.post("/v1/libraries", json={"name": name, "root_path": path})
    data = resp.json()
    library_id = data.get("library_id", "")
    console.print(f"[green]Library created: {library_id}[/green]")
    console.print(f"  name: {data.get('name', name)}")
    console.print(f"  root_path: {data.get('root_path', path)}")


@library_app.command("list")
def library_list() -> None:
    """List all libraries for the current tenant (trashed libraries are hidden by default)."""
    client = LumiverbClient()
    resp = client.get("/v1/libraries")
    libraries = resp.json()
    table = Table(title="Libraries")
    table.add_column("ID", style="dim")
    table.add_column("Name")
    table.add_column("Root path")
    table.add_column("Last ingest")
    for lib in libraries:
        table.add_row(
            lib.get("library_id", ""),
            lib.get("name", ""),
            lib.get("root_path", ""),
            lib.get("last_scan_at") or "—",
        )
    console.print(table)


@library_app.command("update")
def library_update(
    name: Annotated[str, typer.Option("--name", "-n", help="Library name to update.")],
    root_path: Annotated[str | None, typer.Option("--root-path", "-p", help="New root path on disk.")] = None,
    new_name: Annotated[str | None, typer.Option("--new-name", help="New library name.")] = None,
) -> None:
    """Update a library's root path or name."""
    if root_path is None and new_name is None:
        console.print("[red]Provide at least --root-path or --new-name[/red]")
        raise typer.Exit(1)
    client = LumiverbClient()
    resp = client.get("/v1/libraries")
    libraries = resp.json()
    match = next((lib for lib in libraries if lib.get("name") == name), None)
    if match is None:
        console.print(f"[red]Library not found: {name}[/red]")
        raise typer.Exit(1)
    library_id = match["library_id"]
    payload: dict[str, str] = {}
    if root_path is not None:
        payload["root_path"] = root_path
    if new_name is not None:
        payload["name"] = new_name
    resp = client.patch(f"/v1/libraries/{library_id}", json=payload)
    data = resp.json()
    console.print(f"[green]Library updated: {library_id}[/green]")
    console.print(f"  name: {data.get('name')}")
    console.print(f"  root_path: {data.get('root_path')}")


@library_app.command("delete")
def library_delete(
    name: Annotated[
        str,
        typer.Option("--name", "-n", help="Library name to move to trash."),
    ],
) -> None:
    """Move a library to trash (soft delete). Use 'lumiverb library empty-trash' to permanently delete."""
    client = LumiverbClient()
    resp = client.get("/v1/libraries")
    libraries = resp.json()
    match = next((lib for lib in libraries if lib.get("name") == name), None)
    if match is None:
        console.print(f"[red]Library not found: {name}[/red]")
        raise typer.Exit(1)
    library_id = match["library_id"]
    confirm = typer.confirm(
        f"Delete library '{name}'? This moves it to trash. [y/N]",
        default=False,
    )
    if not confirm:
        console.print("Aborted.")
        raise typer.Exit(0)
    client.delete(f"/v1/libraries/{library_id}")
    console.print(f"Library '{name}' moved to trash.")
    console.print("Run 'lumiverb library empty-trash' to permanently delete.")


@library_app.command("empty-trash")
def library_empty_trash() -> None:
    """Permanently delete all libraries in trash and their assets."""
    client = LumiverbClient()
    resp = client.get("/v1/libraries", params={"include_trashed": True})
    libraries = resp.json()
    trashed = [lib for lib in libraries if lib.get("status") == "trashed"]
    if not trashed:
        console.print("Trash is empty.")
        raise typer.Exit(0)
    for lib in trashed:
        console.print(f"  {lib.get('name', '')} ({lib.get('library_id', '')})")
    confirm = typer.confirm(
        f"Permanently delete {len(trashed)} libraries and all their assets? [y/N]",
        default=False,
    )
    if not confirm:
        console.print("Aborted.")
        raise typer.Exit(0)
    empty_resp = client.post("/v1/libraries/empty-trash")
    data = empty_resp.json()
    n = data.get("deleted", 0)
    console.print(f"Deleted {n} libraries.")


# ---------------------------------------------------------------------------
# tenant
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# filter
# ---------------------------------------------------------------------------


def _resolve_library_id_for_filter(client: LumiverbClient, library: str) -> str:
    """Resolve library name to ID."""
    libraries = client.get("/v1/libraries").json()
    match = next((lib for lib in libraries if lib["name"] == library), None)
    if match is None:
        console.print(f"[red]Library not found: {library}[/red]")
        raise typer.Exit(1)
    return match["library_id"]


@filter_app.command("list")
def filter_list(
    library: Annotated[str | None, typer.Option("--library", "-l", help="Library name. Omit to show tenant defaults.")] = None,
) -> None:
    """List filters for a library or tenant defaults."""
    client = LumiverbClient()
    if library:
        library_id = _resolve_library_id_for_filter(client, library)
        resp = client.get(f"/v1/libraries/{library_id}/filters")
        data = resp.json()
        title = f"Filters for library: {library}"
    else:
        resp = client.get("/v1/tenant/filter-defaults")
        data = resp.json()
        title = "Tenant default filters"

    table = Table(title=title)
    table.add_column("ID", style="dim")
    table.add_column("Type")
    table.add_column("Pattern")
    includes = data.get("includes", [])
    excludes = data.get("excludes", [])
    for f in includes:
        table.add_row(f.get("filter_id") or f.get("default_id", ""), "include", f["pattern"])
    for f in excludes:
        table.add_row(f.get("filter_id") or f.get("default_id", ""), "exclude", f["pattern"])
    if not includes and not excludes:
        console.print(f"No filters configured ({title.lower()}).")
    else:
        console.print(table)


@filter_app.command("add")
def filter_add(
    pattern: Annotated[str, typer.Argument(help="Glob pattern (e.g. '**/Output/**').")],
    library: Annotated[str | None, typer.Option("--library", "-l", help="Library name. Omit to add as tenant default.")] = None,
    include: Annotated[bool, typer.Option("--include", help="Add as include filter.")] = False,
    exclude: Annotated[bool, typer.Option("--exclude", help="Add as exclude filter.")] = False,
) -> None:
    """Add a path filter. Specify --include or --exclude."""
    if include == exclude:
        console.print("[red]Specify exactly one of --include or --exclude.[/red]")
        raise typer.Exit(1)
    filter_type = "include" if include else "exclude"

    client = LumiverbClient()
    if library:
        library_id = _resolve_library_id_for_filter(client, library)
        resp = client.post(
            f"/v1/libraries/{library_id}/filters",
            json={"type": filter_type, "pattern": pattern},
        )
        data = resp.json()
        console.print(f"[green]Added {filter_type} filter to library {library}:[/green] {pattern}")
        console.print(f"  ID: {data.get('filter_id') or data.get('default_id', '')}")
    else:
        resp = client.post(
            "/v1/tenant/filter-defaults",
            json={"type": filter_type, "pattern": pattern},
        )
        data = resp.json()
        console.print(f"[green]Added tenant default {filter_type} filter:[/green] {pattern}")
        console.print(f"  ID: {data.get('default_id', '')}")


@filter_app.command("remove")
def filter_remove(
    filter_id: Annotated[str, typer.Argument(help="Filter ID to remove (lpf_... or tpfd_...).")],
    library: Annotated[str | None, typer.Option("--library", "-l", help="Library name. Omit to remove tenant default.")] = None,
) -> None:
    """Remove a filter by ID."""
    client = LumiverbClient()
    if library:
        library_id = _resolve_library_id_for_filter(client, library)
        client.delete(f"/v1/libraries/{library_id}/filters/{filter_id}")
        console.print(f"[green]Removed filter {filter_id} from library {library}.[/green]")
    else:
        client.delete(f"/v1/tenant/filter-defaults/{filter_id}")
        console.print(f"[green]Removed tenant default filter {filter_id}.[/green]")



admin_app = typer.Typer(help="Admin operations (tenant provisioning, API keys). Requires admin key.")
app.add_typer(admin_app, name="admin")
admin_keys_app = typer.Typer(help="Manage API keys for a tenant.")
admin_app.add_typer(admin_keys_app, name="keys")
admin_tenants_app = typer.Typer(help="Manage tenants.")
admin_app.add_typer(admin_tenants_app, name="tenants")


# ---------------------------------------------------------------------------
# admin maintenance
# ---------------------------------------------------------------------------


@admin_app.command("maintenance")
def admin_maintenance(
    start: Annotated[
        bool,
        typer.Option("--start", help="Enable maintenance mode."),
    ] = False,
    end: Annotated[
        bool,
        typer.Option("--end", help="Disable maintenance mode."),
    ] = False,
    message: Annotated[
        str,
        typer.Option("--message", "-m", help="Reason shown in status (used with --start)."),
    ] = "",
) -> None:
    """Show, enable, or disable tenant maintenance mode.

    With no flags: show current status.
    With --start [--message '...']: enable maintenance mode (workers stop claiming jobs).
    With --end: disable maintenance mode.
    """
    if start and end:
        console.print("[red]--start and --end are mutually exclusive.[/red]")
        raise typer.Exit(1)

    client = LumiverbClient()

    if start:
        resp = client.post("/v1/tenant/maintenance/start", json={"message": message})
        data = resp.json()
        console.print(f"[yellow]Maintenance mode enabled.[/yellow]")
        if data.get("message"):
            console.print(f"  Message:    {data['message']}")
        console.print(f"  Started at: {data.get('started_at', '')}")
        return

    if end:
        client.post("/v1/tenant/maintenance/end", json={})
        console.print("[green]Maintenance mode disabled.[/green]")
        return

    # No flags — show status.
    resp = client.get("/v1/tenant/maintenance/status")
    data = resp.json()
    active = data.get("active", False)
    if active:
        console.print(f"[yellow]Maintenance mode: ACTIVE[/yellow]")
        if data.get("message"):
            console.print(f"  Message:    {data['message']}")
        if data.get("started_at"):
            console.print(f"  Started at: {data['started_at']}")
    else:
        console.print("[green]Maintenance mode: inactive[/green]")


def _require_admin_key(
    admin_key: str | None,
) -> str:
    """Return admin key or exit with error if missing."""
    key = admin_key or _get_env_admin_key()
    if not key:
        typer.echo(
            "Admin key required. Use --admin-key or set LUMIVERB_ADMIN_KEY.",
            err=True,
        )
        raise typer.Exit(1)
    return key


def _get_env_admin_key() -> str | None:
    """Read LUMIVERB_ADMIN_KEY from environment."""
    import os
    return os.environ.get("LUMIVERB_ADMIN_KEY") or None


# ---------------------------------------------------------------------------
# admin keys
# ---------------------------------------------------------------------------


@admin_keys_app.command("create")
def admin_keys_create(
    tenant_id: Annotated[str, typer.Option("--tenant-id", help="Tenant ID (e.g. ten_xxx).")],
    name: Annotated[str, typer.Option("--name", "-n", help="Human-readable label for the key (e.g. robert-macbook).")],
    admin_key: Annotated[
        str | None,
        typer.Option("--admin-key", envvar="LUMIVERB_ADMIN_KEY", help="Admin key for API auth."),
    ] = None,
) -> None:
    """Create a new API key for a tenant. The raw key is printed once and never stored."""
    key = _require_admin_key(admin_key)
    client = LumiverbClient(api_key_override=key)
    resp = client.post(
        f"/v1/admin/tenants/{tenant_id}/keys",
        json={"name": name},
    )
    data = resp.json()
    raw_key = data.get("api_key", "")
    console.print(f"[green]API key created.[/green]")
    console.print(raw_key)


@admin_keys_app.command("list")
def admin_keys_list(
    tenant_id: Annotated[str, typer.Option("--tenant-id", help="Tenant ID (e.g. ten_xxx).")],
    admin_key: Annotated[
        str | None,
        typer.Option("--admin-key", envvar="LUMIVERB_ADMIN_KEY", help="Admin key for API auth."),
    ] = None,
) -> None:
    """List API key metadata for a tenant (name, created_at). Raw keys are never shown."""
    key = _require_admin_key(admin_key)
    client = LumiverbClient(api_key_override=key)
    resp = client.get(f"/v1/admin/tenants/{tenant_id}/keys")
    keys = resp.json()
    table = Table(title=f"Keys for tenant {tenant_id}")
    table.add_column("Name")
    table.add_column("Created")
    for k in keys:
        table.add_row(k.get("name", ""), k.get("created_at", ""))
    console.print(table)


# ---------------------------------------------------------------------------
# admin tenants
# ---------------------------------------------------------------------------


@admin_tenants_app.command("list")
def admin_tenants_list(
    admin_key: Annotated[
        str | None,
        typer.Option("--admin-key", envvar="LUMIVERB_ADMIN_KEY", help="Admin key for API auth."),
    ] = None,
) -> None:
    """List all tenants with id, name, plan, status."""
    key = _require_admin_key(admin_key)
    client = LumiverbClient(api_key_override=key)
    resp = client.get("/v1/admin/tenants")
    tenants = resp.json()
    table = Table(title="Tenants")
    table.add_column("Tenant ID", style="dim")
    table.add_column("Name")
    table.add_column("Plan")
    table.add_column("Status")
    for t in tenants:
        table.add_row(
            t.get("tenant_id", ""),
            t.get("name", ""),
            t.get("plan", ""),
            t.get("status", ""),
        )
    console.print(table)


@admin_tenants_app.command("set-vision")
def admin_tenant_set_vision(
    tenant_id: Annotated[str, typer.Option("--tenant-id", "-t", help="Tenant ID.")],
    vision_api_url: Annotated[str | None, typer.Option("--vision-api-url", help="OpenAI-compatible vision API base URL.")] = None,
    vision_api_key: Annotated[str | None, typer.Option("--vision-api-key", help="API key for the vision endpoint.")] = None,
    vision_model_id: Annotated[str | None, typer.Option("--vision-model-id", help="Vision model ID (default: auto-discover from API).")] = None,
    admin_key: Annotated[str | None, typer.Option("--admin-key", envvar="LUMIVERB_ADMIN_KEY", help="Admin key for API auth.")] = None,
) -> None:
    """Set the vision API URL, key, and/or model ID for a tenant."""
    if vision_api_url is None and vision_api_key is None and vision_model_id is None:
        console.print("[red]Provide at least one of --vision-api-url, --vision-api-key, or --vision-model-id.[/red]")
        raise typer.Exit(1)
    key = _require_admin_key(admin_key)
    client = LumiverbClient(api_key_override=key)
    body: dict = {}
    if vision_api_url is not None:
        body["vision_api_url"] = vision_api_url
    if vision_api_key is not None:
        body["vision_api_key"] = vision_api_key
    if vision_model_id is not None:
        body["vision_model_id"] = vision_model_id
    resp = client.patch(f"/v1/admin/tenants/{tenant_id}", json=body)
    data = resp.json()
    console.print(f"[green]Tenant {data['tenant_id']} updated.[/green]")
    console.print(f"  vision_api_url: {data['vision_api_url']}")
    if data.get("vision_model_id"):
        console.print(f"  vision_model_id: {data['vision_model_id']}")


# ---------------------------------------------------------------------------
# admin vision-test
# ---------------------------------------------------------------------------


@admin_app.command("vision-test")
def admin_vision_test(
    path: Annotated[
        Path,
        typer.Option("--path", help="Directory containing test images."),
    ],
    url: Annotated[
        str | None,
        typer.Option("--url", help="Vision API base URL (default: configured URL)."),
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Vision API key (default: configured key)."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Model ID (default: configured/auto-discovered model)."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output JSON path (default: <path>/vision-test-<timestamp>.json)."),
    ] = None,
) -> None:
    """Run describe + OCR against every image in a directory and save results as JSON.

    Useful for benchmarking vision models: save one run as baseline.json,
    then compare future runs to see how model changes affect output.
    """
    from datetime import datetime, timezone

    from src.cli.ingest import _resolve_vision_config
    from src.core.file_extensions import IMAGE_EXTENSIONS
    from src.workers.captions.factory import get_caption_provider

    path = path.expanduser().resolve()
    if not path.is_dir():
        console.print(f"[red]Not a directory: {path}[/red]")
        raise typer.Exit(1)

    # Collect image files (sorted alphabetically)
    images = sorted(
        [f for f in path.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS],
        key=lambda f: f.name.lower(),
    )
    if not images:
        console.print(f"[yellow]No images found in {path}[/yellow]")
        raise typer.Exit(0)

    # Resolve vision config (CLI config > tenant config > auto-discover)
    client = LumiverbClient()
    vision_url, vision_key, model_id, source = _resolve_vision_config(client)

    # --api-key override
    if api_key:
        vision_key = api_key

    # --url override
    if url:
        vision_url = url.rstrip("/")

    # --model override; fall back to auto-discover if url changed
    if model:
        model_id = model
    elif url:
        from src.workers.captions.model_discovery import discover_model_id
        model_id = discover_model_id(vision_url, vision_key)

    if not vision_url:
        console.print("[red]No --url provided and no vision API configured (client or tenant).[/red]")
        raise typer.Exit(1)

    provider = get_caption_provider(model_id, vision_url, vision_key)

    # Default output path
    if output is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        output = path / f"vision-test-{ts}.json"

    console.print(f"Model: [cyan]{model_id}[/cyan]  URL: [cyan]{vision_url}[/cyan]")
    console.print(f"Images: [cyan]{len(images)}[/cyan]  Output: [cyan]{output}[/cyan]")
    console.print()

    results: list[dict] = []
    for i, img_path in enumerate(images, 1):
        console.print(f"[dim][{i}/{len(images)}][/dim] {img_path.name} ... ", end="")

        # Describe (description + tags)
        desc_result = provider.describe(img_path)
        description = desc_result.get("description", "")
        tags = desc_result.get("tags", [])

        # OCR
        ocr_text = provider.extract_text(img_path)

        results.append({
            "filename": img_path.name,
            "description": description,
            "tags": tags,
            "ocr": ocr_text,
        })
        console.print("[green]done[/green]")

    output_data = {
        "model_id": model_id,
        "vision_api_url": vision_url,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "image_count": len(results),
        "results": results,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_json.dumps(output_data, indent=2, ensure_ascii=False) + "\n")
    console.print(f"\n[green]Wrote {output}[/green]")


# ---------------------------------------------------------------------------
# worker (continued)
# ---------------------------------------------------------------------------


def _resolve_library_id(client: object, library_name: str) -> str:
    """Resolve library name to library_id. Exits with 1 if not found."""
    libraries = client.get("/v1/libraries").json()
    match = next((l for l in libraries if l.get("name") == library_name), None)
    if match is None:
        typer.echo(f"Library not found: {library_name}", err=True)
        raise typer.Exit(1)
    return match["library_id"]


def _resolve_library(client: object, library_name: str) -> dict:
    """Resolve library name to full library dict (library_id, name, root_path, ...). Exits with 1 if not found."""
    libraries = client.get("/v1/libraries").json()
    match = next((l for l in libraries if l.get("name") == library_name), None)
    if match is None:
        typer.echo(f"Library not found: {library_name}", err=True)
        raise typer.Exit(1)
    return match


def _resolve_asset_id(
    client: object,
    library_id: str,
    asset_id: str | None,
    path: str | None,
) -> str:
    """
    Resolve --asset-id or --path to a concrete asset_id.
    Exactly one of asset_id or path must be provided.
    Raises typer.Exit(1) with an error message if path is a directory,
    if neither is provided, or if both are provided.
    """
    if asset_id and path:
        console.print("[red]--asset-id and --path are mutually exclusive[/red]")
        raise typer.Exit(1)
    if not asset_id and not path:
        console.print("[red]One of --asset-id or --path is required[/red]")
        raise typer.Exit(1)
    if asset_id:
        return asset_id

    norm = normalize_path_prefix(path)
    if norm is None:
        console.print("[red]Invalid path[/red]")
        raise typer.Exit(1)

    if "." not in Path(norm).name:
        console.print(
            f"[red]Path '{norm}' looks like a directory. This command operates on a single asset. Use --path with a file path.[/red]"
        )
        raise typer.Exit(1)

    resp = client.get(
        "/v1/assets/by-path",
        params={"library_id": library_id, "rel_path": norm},
    )
    if resp.status_code == 404:
        console.print(f"[red]Asset not found: {norm}[/red]")
        raise typer.Exit(1)
    resp.raise_for_status()
    return resp.json()["asset_id"]


@app.command("download")
def download(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    asset_id: Annotated[
        str | None,
        typer.Option("--asset-id", help="Asset ID to download."),
    ] = None,
    path: Annotated[
        str | None,
        typer.Option("--path", "-p", help="Relative path to asset file within the library."),
    ] = None,
    size: Annotated[
        str,
        typer.Option("--size", "-s", help="Which file to download: proxy or thumbnail."),
    ] = "proxy",
    output: Annotated[
        str | None,
        typer.Option(
            "--output",
            "-o",
            help="Output file path. Omit to stream to stdout (pipe only).",
        ),
    ] = None,
) -> None:
    """Download a proxy or thumbnail image for an asset.

    Output to a file with --output, or pipe to another command (e.g. | viu -).
    Writing binary to a terminal is not allowed.
    """
    import os
    import sys

    if size not in ("proxy", "thumbnail"):
        console.print("[red]--size must be one of: proxy, thumbnail[/red]")
        raise typer.Exit(1)

    # Guard: refuse to write binary to a TTY
    stdout_is_tty = sys.stdout.isatty()
    if output is None and stdout_is_tty:
        console.print(
            "[red]Refusing to write binary to terminal.[/red]\n"
            "Use [bold]--output <path>[/bold] to save to a file, "
            "or pipe to another command (e.g. [bold]| viu -[/bold])."
        )
        raise typer.Exit(1)

    client = LumiverbClient()
    library_id = _resolve_library_id(client, library)
    resolved_asset_id = _resolve_asset_id(client, library_id, asset_id, path)

    # Stream the response so we don't buffer the whole file in memory
    with client.stream(f"/v1/assets/{resolved_asset_id}/{size}") as resp:
        if resp.status_code == 404:
            console.print(f"[red]No {size} available for asset {resolved_asset_id}[/red]")
            raise typer.Exit(1)

        resp.raise_for_status()

        if output:
            out_path = Path(output)

            # If output is a directory (existing or implied by trailing slash), derive filename.
            output_str = str(output)
            is_dir_hint = output_str.endswith(("/", "\\")) or out_path.is_dir()
            if is_dir_hint:
                # Ensure directory exists before deriving filename.
                out_dir = out_path
                out_dir.mkdir(parents=True, exist_ok=True)

                # derive filename from asset rel_path
                asset_resp = client.get(f"/v1/assets/{resolved_asset_id}").json()
                stem = Path(asset_resp["rel_path"]).stem
                out_path = out_dir / f"{stem}_{size}.jpg"
            else:
                out_path.parent.mkdir(parents=True, exist_ok=True)

            with open(out_path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    f.write(chunk)
            console.print(f"Saved to {out_path}")
        else:
            # Pipe mode: stream bytes to stdout
            for chunk in resp.iter_bytes(chunk_size=65536):
                sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()


@app.command("scan")
def scan(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    path_prefix: Annotated[str | None, typer.Option("--path-prefix", "-p", help="Only scan files under this subdirectory.")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Re-scan unchanged files (re-generate proxy even if SHA matches).")] = False,
    concurrency: Annotated[int, typer.Option("--concurrency", help="Number of parallel workers.")] = 4,
    media_type: Annotated[str, typer.Option("--media-type", help="Filter: image, video, or all.")] = "all",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would happen without making changes.")] = False,
) -> None:
    """Discover files, compute SHA, extract EXIF, generate proxies, upload.

    Scan is the first phase of the scan/enrich pipeline. It touches source
    files, generates 2048px proxies, and uploads them to the server. It does
    NOT run enrichment (CLIP, vision, OCR, faces) — use `lumiverb enrich`
    or `lumiverb repair` for that.

    Change detection compares source file SHA-256 against the server to
    skip unchanged files. Use --force to re-scan everything.
    """
    from src.cli.scan import run_scan

    if media_type not in ("image", "video", "all"):
        console.print(f"[red]Invalid --media-type: {media_type}. Must be image, video, or all.[/red]")
        raise typer.Exit(1)

    client = LumiverbClient()
    libraries = client.get("/v1/libraries").json()
    match = next((lib for lib in libraries if lib["name"] == library), None)
    if match is None:
        console.print(f"[red]Library not found: {library}[/red]")
        raise typer.Exit(1)

    stats = run_scan(
        client,
        match,
        concurrency=concurrency,
        path_prefix=path_prefix,
        force=force,
        media_type_filter=media_type,
        dry_run=dry_run,
        console=console,
    )

    if not dry_run:
        console.print(
            f"\nDone: {stats.new:,} new, "
            f"{stats.changed:,} changed, "
            f"{stats.unchanged:,} unchanged, "
            f"{stats.deleted:,} deleted"
            + (f", {stats.cache_populated:,} cache populated" if stats.cache_populated else "")
            + (f", {stats.failed:,} failed" if stats.failed else "")
        )

        # Show what enrichment is pending
        from src.cli.repair import get_repair_summary
        summary = get_repair_summary(client, match["library_id"])
        needs_enrich = [
            (label, summary.get(key, 0))
            for label, key in [
                ("Vision AI", "missing_vision"),
                ("Embeddings", "missing_embeddings"),
                ("Faces", "missing_faces"),
                ("OCR", "missing_ocr"),
                ("Search sync", "stale_search_sync"),
            ]
            if summary.get(key, 0) > 0
        ]
        if needs_enrich:
            console.print("\n[yellow]Pending enrichment:[/yellow]")
            for label, count in needs_enrich:
                console.print(f"  {label}: {count:,}")
            console.print(f"[dim]Run: lumiverb enrich --library {library}[/dim]")

        if stats.failed > 0:
            raise typer.Exit(1)


ENRICH_TYPES = ("embed", "vision", "faces", "redetect-faces", "ocr", "video-scenes", "scene-vision", "search-sync", "all")


@app.command("enrich")
def enrich(
    library: Annotated[str | None, typer.Option("--library", "-l", help="Library name (omit to enrich all libraries).")] = None,
    job_type: Annotated[str, typer.Option("--job-type", "-j", help="Enrichment type.")] = "all",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be enriched without making changes.")] = False,
    concurrency: Annotated[int, typer.Option("--concurrency", help="Number of parallel workers.")] = 4,
    force: Annotated[bool, typer.Option("--force", help="Force full re-processing (search-sync: clear timestamps and re-index all).")] = False,
) -> None:
    """Run enrichment on assets with missing pipeline outputs.

    Reads proxies from the local cache (populated by scan) and runs
    inference. On cache miss, downloads the proxy from the server.

    \b
    Job types:
      embed           — Generate missing CLIP embeddings (similarity search)
      vision          — Generate missing AI descriptions and tags
      faces           — Detect faces using InsightFace (face recognition)
      redetect-faces  — Re-run face detection on ALL images with quality gates
      ocr             — Extract text from images via vision AI
      video-scenes    — Run scene detection on unindexed videos
      scene-vision    — Extract rep frames + run vision AI on scenes
      search-sync     — Push stale assets to Quickwit search index
      all             — Run all enrichment in logical order (default)
    """
    from src.cli.repair import run_repair

    if job_type not in ENRICH_TYPES:
        console.print(f"[red]Invalid --job-type: {job_type}. Must be one of: {', '.join(ENRICH_TYPES)}[/red]")
        raise typer.Exit(1)

    client = LumiverbClient()
    libraries = client.get("/v1/libraries").json()

    if library is not None:
        targets = [lib for lib in libraries if lib["name"] == library]
        if not targets:
            console.print(f"[red]Library not found: {library}[/red]")
            raise typer.Exit(1)
    else:
        targets = libraries
        if not targets:
            console.print("No libraries found.")
            return

    for lib in targets:
        run_repair(
            client,
            lib,
            job_type=job_type,
            dry_run=dry_run,
            concurrency=concurrency,
            force=force,
            console=console,
        )


@app.command()
def search(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name")],
    query: Annotated[str, typer.Option("--query", "-q", help="Search query")],
    output: Annotated[str, typer.Option("--output", "-o", help="Output format: table, json, text")] = "table",
    media_type: Annotated[str, typer.Option("--media-type", "-m", help="Filter by type: image, video, all")] = "all",
    limit: Annotated[int, typer.Option("--limit", help="Max results (0 = all)")] = 20,
    offset: Annotated[int, typer.Option("--offset", help="Start offset")] = 0,
) -> None:
    """Search assets and video scenes in a library by natural language query."""
    if output not in ("table", "json", "text"):
        console.print("[red]--output must be one of: table, json, text[/red]")
        raise typer.Exit(1)

    if media_type not in ("image", "video", "all"):
        console.print("[red]--media-type must be one of: image, video, all[/red]")
        raise typer.Exit(1)

    client = LumiverbClient()
    library_id = _resolve_library_id(client, library)

    if limit == 0:
        all_hits: list[dict] = []
        page_offset = offset
        page_size = 100
        source = "unknown"
        while True:
            resp = client.get(
                "/v1/search",
                params={
                    "library_id": library_id,
                    "q": query,
                    "limit": page_size,
                    "offset": page_offset,
                    "media_type": media_type,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            source = data.get("source", "unknown")
            batch = data.get("hits", [])
            all_hits.extend(batch)
            if len(batch) < page_size:
                break
            page_offset += page_size
        hits = all_hits
    else:
        resp = client.get(
            "/v1/search",
            params={
                "library_id": library_id,
                "q": query,
                "limit": limit,
                "offset": offset,
                "media_type": media_type,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        hits = data.get("hits", [])
        source = data.get("source", "unknown")

    if not hits:
        console.print("No results.")
        return

    if output == "json":
        import sys as _sys
        _sys.stdout.write(_json.dumps(hits, indent=2))
        _sys.stdout.write("\n")

    elif output == "text":
        for hit in hits:
            console.print(hit["rel_path"])

    else:  # table (default)
        table = Table(show_header=True, show_lines=False)
        table.add_column("Type", style="dim", width=6)
        table.add_column("Path", style="cyan", no_wrap=False)
        table.add_column("Detail", no_wrap=False, max_width=40)
        table.add_column("Description", no_wrap=False, max_width=100)
        table.add_column("Tags", no_wrap=False, max_width=60)

        for hit in hits:
            hit_type = hit.get("type", "image")
            tags_str = ", ".join(hit.get("tags") or [])
            description = hit.get("description") or ""
            desc_display = description[:100] + "…" if len(description) > 100 else description
            tags_display = tags_str[:60] + "…" if len(tags_str) > 60 else tags_str

            if hit_type == "scene":
                start_s = (hit.get("start_ms") or 0) // 1000
                end_s = (hit.get("end_ms") or 0) // 1000
                detail = f"{start_s}s – {end_s}s"
                type_label = "[magenta]scene[/magenta]"
            else:
                detail = hit.get("camera_model") or hit.get("camera_make") or ""
                type_label = "[blue]image[/blue]"

            table.add_row(
                type_label,
                hit["rel_path"],
                detail,
                desc_display,
                tags_display,
            )

        console.print(table)
        n = len(hits)
        console.print(f"[dim]{n} result(s) via {source}[/dim]")


# ---------------------------------------------------------------------------
# similar
# ---------------------------------------------------------------------------


@app.command()
def similar(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    asset_id: Annotated[str | None, typer.Option("--asset-id", help="Asset ID to find similar assets for.")] = None,
    path: Annotated[str | None, typer.Option("--path", "-p", help="Relative path to asset within the library.")] = None,
    image: Annotated[Path | None, typer.Option("--image", "-i", help="Path to a local image file to search by.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max similar assets to return.")] = 20,
    offset: Annotated[int, typer.Option("--offset", help="Start offset for pagination.")] = 0,
    output: Annotated[str, typer.Option("--output", "-o", help="Output format: table, json, text")] = "table",
    from_ts: Annotated[float | None, typer.Option("--from-ts", help="Filter: minimum taken_at timestamp.")] = None,
    to_ts: Annotated[float | None, typer.Option("--to-ts", help="Filter: maximum taken_at timestamp.")] = None,
    asset_types: Annotated[str | None, typer.Option("--asset-types", help="Filter: image,video")] = None,
    camera_make: Annotated[list[str] | None, typer.Option("--camera-make", help="Filter by camera make.")] = None,
    camera_model: Annotated[list[str] | None, typer.Option("--camera-model", help="Filter by camera model.")] = None,
) -> None:
    """Find visually similar assets by vector similarity.

    Supply one of: --asset-id, --path (existing asset), or --image (local file).
    """
    if output not in ("table", "json", "text"):
        console.print("[red]--output must be one of: table, json, text[/red]")
        raise typer.Exit(1)

    sources = sum(1 for s in (asset_id, path, image) if s is not None)
    if sources == 0:
        console.print("[red]Provide one of: --asset-id, --path, or --image[/red]")
        raise typer.Exit(1)
    if sources > 1:
        console.print("[red]Provide only one of: --asset-id, --path, or --image[/red]")
        raise typer.Exit(1)

    client = LumiverbClient()
    library_id = _resolve_library_id(client, library)

    if image is not None:
        # Search by local image file
        _similar_by_image(
            client, library_id, image, limit, offset, output,
            from_ts, to_ts, asset_types, camera_make, camera_model,
        )
    else:
        # Search by existing asset
        _similar_by_asset(client, library_id, asset_id, path, limit, offset, output)


def _similar_by_asset(
    client: LumiverbClient,
    library_id: str,
    asset_id: str | None,
    path: str | None,
    limit: int,
    offset: int,
    output: str,
) -> None:
    resolved_asset_id = _resolve_asset_id(client, library_id=library_id, asset_id=asset_id, path=path)
    resp = client.get(
        "/v1/similar",
        params={"asset_id": resolved_asset_id, "library_id": library_id, "limit": limit, "offset": offset},
    )
    data = resp.json()
    hits = data.get("hits", [])

    if not data.get("embedding_available", False) and not hits:
        console.print("No similar assets (source asset has no embeddings).")
        return
    if not hits:
        console.print("No similar assets.")
        return

    _print_similar_results(hits, data, output, len(hits))


def _similar_by_image(
    client: LumiverbClient,
    library_id: str,
    image_path: Path,
    limit: int,
    offset: int,
    output: str,
    from_ts: float | None,
    to_ts: float | None,
    asset_types: str | None,
    camera_make: list[str] | None,
    camera_model: list[str] | None,
) -> None:
    import base64
    import io
    from PIL import Image as PILImage

    pil_img = PILImage.open(image_path).convert("RGB")
    w, h = pil_img.size
    scale = 2048 / max(w, h)
    if scale < 1.0:
        pil_img = pil_img.resize((int(w * scale), int(h * scale)), PILImage.LANCZOS)

    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=85)
    image_b64 = base64.b64encode(buf.getvalue()).decode()

    cameras = None
    if camera_make or camera_model:
        makes = camera_make or []
        models = camera_model or []
        n = max(len(makes), len(models))
        cameras = [
            {"make": makes[i] if i < len(makes) else None, "model": models[i] if i < len(models) else None}
            for i in range(n)
            if (makes[i] if i < len(makes) else None) or (models[i] if i < len(models) else None)
        ]

    asset_types_list = None
    if asset_types:
        allowed = {"image", "video"}
        asset_types_list = [t.strip() for t in asset_types.split(",") if t.strip() in allowed] or None

    payload: dict = {"library_id": library_id, "image_b64": image_b64, "limit": limit, "offset": offset}
    if from_ts is not None:
        payload["from_ts"] = from_ts
    if to_ts is not None:
        payload["to_ts"] = to_ts
    if asset_types_list:
        payload["asset_types"] = asset_types_list
    if cameras:
        payload["cameras"] = cameras

    resp = client.post("/v1/similar/search-by-image", json=payload)
    resp.raise_for_status()
    data = resp.json()
    hits = data.get("hits", [])
    total = data.get("total", 0)

    _print_similar_results(hits, data, output, total)


def _print_similar_results(hits: list, data: dict, output: str, total: int) -> None:
    if not hits:
        console.print("No similar assets.")
        return

    if output == "json":
        sys.stdout.write(_json.dumps(data, indent=2))
        sys.stdout.write("\n")
    elif output == "text":
        for hit in hits:
            console.print(hit["rel_path"])
    else:
        table = Table(show_header=True, show_lines=False)
        table.add_column("Path", style="cyan", no_wrap=False)
        table.add_column("Distance", justify="right", style="dim", width=10)
        table.add_column("Asset ID", style="dim", width=28)
        for hit in hits:
            table.add_row(hit.get("rel_path", ""), f"{hit.get('distance', 0.0):.4f}", hit.get("asset_id", ""))
        console.print(table)
        console.print(f"[dim]{total} result(s)[/dim]")


def main() -> None:
    """Entry point for the lumiverb script."""
    try:
        app()
    except LumiverbAPIError:
        # Error message already printed to stderr by the client; just exit non-zero.
        sys.exit(1)


