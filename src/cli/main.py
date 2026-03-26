"""Typer CLI entry point: config, library create/list, scan (stub)."""

from enum import Enum
from pathlib import Path
from typing import Annotated

from datetime import datetime, timezone

import json as _json
import logging
import sys
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from src.cli.client import LumiverbAPIError, LumiverbClient
from src.cli.config import get_admin_key, load_config, save_config
from src.cli.commands.keys import keys_app
from src.cli.commands import users as users_commands
from src.cli.scanner import (
    APPLY_FILTERS_BATCH_SIZE,
    SCAN_PAGE_SIZE,
    _load_path_filters,
    fetch_filter_candidates,
    scan_library,
)
from src.core.io_utils import normalize_path_prefix
from src.core.logging_config import configure_logging

_log = logging.getLogger(__name__)

app = typer.Typer()
config_app = typer.Typer(help="Manage API URL and API key.")
app.add_typer(config_app, name="config")
library_app = typer.Typer(help="Create and list libraries.")
app.add_typer(library_app, name="library")
app.add_typer(keys_app, name="keys")
users_commands.register(app)
tenant_app = typer.Typer(help="Manage tenants (admin only).")
app.add_typer(tenant_app, name="tenant")

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
    table.add_column("Scan status")
    table.add_column("Vision Model")
    table.add_column("Last scan")
    for lib in libraries:
        table.add_row(
            lib.get("library_id", ""),
            lib.get("name", ""),
            lib.get("root_path", ""),
            lib.get("scan_status", ""),
            lib.get("vision_model_id", ""),
            lib.get("last_scan_at") or "—",
        )
    console.print(table)


@library_app.command("set-model")
def library_set_model(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    model: Annotated[
        str,
        typer.Option(
            "--model",
            "-m",
            help="OpenAI-compatible model ID (e.g. \"qwen3-visioncaption-2b\", \"llava:13b\").",
        ),
    ],
) -> None:
    """Set the vision model for a library."""
    if not model.strip():
        typer.echo("Model ID cannot be empty.")
        raise typer.Exit(1)
    client = LumiverbClient()
    library_id = _resolve_library_id(client, library)
    r = client.patch(f"/v1/libraries/{library_id}", json={"vision_model_id": model})
    r.raise_for_status()
    typer.echo(f"Library {library_id} now uses model: {model}")


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


@tenant_app.command("list")
def tenant_list(
    admin_key: Annotated[str | None, typer.Option("--admin-key", help="Admin key (falls back to saved config).")] = None,
) -> None:
    """List all tenants."""
    key = admin_key or get_admin_key()
    if not key:
        console.print("[red]Admin key required. Use --admin-key or run: lumiverb config set --admin-key <key>[/red]")
        raise typer.Exit(1)
    client = LumiverbClient(api_key_override=key)
    resp = client.get("/v1/admin/tenants")
    tenants = resp.json()
    table = Table(title="Tenants")
    table.add_column("ID", style="dim")
    table.add_column("Name")
    table.add_column("Plan")
    table.add_column("Status")
    for t in tenants:
        table.add_row(t.get("tenant_id", ""), t.get("name", ""), t.get("plan", ""), t.get("status", ""))
    console.print(table)


@tenant_app.command("set-vision")
def tenant_set_vision(
    tenant_id: Annotated[str, typer.Option("--tenant-id", "-t", help="Tenant ID.")],
    vision_api_url: Annotated[str | None, typer.Option("--vision-api-url", help="OpenAI-compatible vision API base URL.")] = None,
    vision_api_key: Annotated[str | None, typer.Option("--vision-api-key", help="API key for the vision endpoint.")] = None,
    admin_key: Annotated[str | None, typer.Option("--admin-key", help="Admin key (falls back to saved config).")] = None,
) -> None:
    """Set the vision API URL and/or key for a tenant."""
    if vision_api_url is None and vision_api_key is None:
        console.print("[red]Provide at least one of --vision-api-url or --vision-api-key.[/red]")
        raise typer.Exit(1)
    key = admin_key or get_admin_key()
    if not key:
        console.print("[red]Admin key required. Use --admin-key or run: lumiverb config set --admin-key <key>[/red]")
        raise typer.Exit(1)
    client = LumiverbClient(api_key_override=key)
    body: dict = {}
    if vision_api_url is not None:
        body["vision_api_url"] = vision_api_url
    if vision_api_key is not None:
        body["vision_api_key"] = vision_api_key
    resp = client.patch(f"/v1/admin/tenants/{tenant_id}", json=body)
    data = resp.json()
    console.print(f"[green]Tenant {data['tenant_id']} updated.[/green]")
    console.print(f"  vision_api_url: {data['vision_api_url']}")


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------

worker_app = typer.Typer(help="Run background workers.")
app.add_typer(worker_app, name="worker")

pipeline_app = typer.Typer(help="Orchestrate pipeline runs (scan + workers) with locking and live dashboard.")
app.add_typer(pipeline_app, name="pipeline")

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
        console.print("[green]Maintenance mode disabled. Workers will resume claiming jobs.[/green]")
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


@worker_app.command("proxy")
def worker_proxy(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    once: bool = typer.Option(False, "--once", help="Process all queued jobs then exit."),
    concurrency: int = typer.Option(1, "--concurrency", help="Number of parallel workers."),
    output: Annotated[
        str,
        typer.Option("--output", help="Output mode: human (default) or jsonl for structured events."),
    ] = "human",
) -> None:
    """Generate proxies and thumbnails for pending image assets."""
    from src.storage.artifact_store import RemoteArtifactStore
    from src.workers.proxy import ProxyWorker

    client = LumiverbClient()
    library_id: str = _resolve_library_id(client, library)

    worker = ProxyWorker(
        client=client,
        artifact_store=RemoteArtifactStore(client=client),
        concurrency=concurrency,
        once=once,
        library_id=library_id,
        output_mode=output,
    )
    worker.run()


@worker_app.command("exif")
def worker_exif(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    once: Annotated[bool, typer.Option("--once")] = False,
    output: Annotated[
        str,
        typer.Option("--output", help="Output mode: human (default) or jsonl for structured events."),
    ] = "human",
) -> None:
    """Run the EXIF metadata worker."""
    from src.workers.exif_worker import ExifWorker

    client = LumiverbClient()
    library_id = _resolve_library_id(client, library)
    worker = ExifWorker(client=client, once=once, library_id=library_id, output_mode=output)
    worker.run()


@worker_app.command("vision")
def worker_vision(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    once: Annotated[bool, typer.Option("--once")] = False,
    output: Annotated[
        str,
        typer.Option("--output", help="Output mode: human (default) or jsonl for structured events."),
    ] = "human",
) -> None:
    """Run the AI vision worker (Moondream descriptions and tags)."""
    from src.storage.artifact_store import RemoteArtifactStore
    from src.workers.vision_worker import VisionWorker

    client = LumiverbClient()
    library_id = _resolve_library_id(client, library)

    worker = VisionWorker(
        client=client,
        artifact_store=RemoteArtifactStore(client=client),
        once=once,
        library_id=library_id,
        output_mode=output,
    )
    worker.run()


@worker_app.command("video-preview")
def worker_video_preview(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    once: Annotated[bool, typer.Option("--once", help="Process queue until empty then exit.")] = False,
    concurrency: Annotated[int, typer.Option("--concurrency", help="Number of parallel workers.")] = 1,
    path: Annotated[str | None, typer.Option("--path", "-p", help="Optional subpath to scope jobs.")] = None,
    output: Annotated[
        str,
        typer.Option("--output", help="Output mode: human (default) or jsonl for structured events."),
    ] = "human",
) -> None:
    """Run the video preview worker (short MP4 previews for video assets)."""
    from src.storage.artifact_store import RemoteArtifactStore
    from src.workers.video_preview_worker import VideoPreviewWorker

    client = LumiverbClient()
    library_id = _resolve_library_id(client, library)
    path_prefix = normalize_path_prefix(path) if path else None

    worker = VideoPreviewWorker(
        client=client,
        artifact_store=RemoteArtifactStore(client=client),
        concurrency=concurrency,
        once=once,
        library_id=library_id,
        path_prefix=path_prefix,
        output_mode=output,
    )
    worker.run()


@worker_app.command("embed")
def worker_embed(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    once: Annotated[bool, typer.Option("--once", help="Process queue until empty then exit.")] = False,
    output: Annotated[
        str,
        typer.Option("--output", help="Output mode: human (default) or jsonl for structured events."),
    ] = "human",
) -> None:
    """Run the embedding worker (CLIP + Moondream vectors for similarity search)."""
    from src.storage.artifact_store import RemoteArtifactStore
    from src.workers.embed_worker import EmbedWorker

    client = LumiverbClient()
    library_id = _resolve_library_id(client, library)

    worker = EmbedWorker(
        client=client,
        artifact_store=RemoteArtifactStore(client=client),
        once=once,
        library_id=library_id,
        output_mode=output,
    )
    worker.run()


@worker_app.command("video-index")
def worker_video_index(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    once: Annotated[bool, typer.Option("--once", help="Process queue until empty then exit.")] = False,
    output: Annotated[
        str,
        typer.Option("--output", help="Output mode: human (default) or jsonl for structured events."),
    ] = "human",
) -> None:
    """Run the video index worker (scene detection for video assets)."""
    import threading
    from pathlib import Path as _Path
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
    from src.storage.artifact_store import RemoteArtifactStore
    from src.workers.video_index_worker import VideoIndexWorker

    client = LumiverbClient()
    library_id = _resolve_library_id(client, library)
    console = Console()
    artifact_store = RemoteArtifactStore(client=client)

    jobs_done = 0
    jobs_failed = 0
    _lock = threading.Lock()

    if output == "jsonl":
        worker = VideoIndexWorker(
            client=client,
            once=once,
            library_id=library_id,
            progress_callback=None,
            artifact_store=artifact_store,
            suppress_base_progress=True,
            output_mode=output,
        )
        worker.run()
    else:
        with Progress(
            SpinnerColumn(),
            BarColumn(),
            TextColumn("[progress.description]{task.description}"),
            TextColumn("  "),
            TextColumn("{task.fields[detail]}"),
            console=console,
            refresh_per_second=10,
        ) as progress:
            job_task = progress.add_task(
                "Processing video-index jobs",
                total=None,
                detail="",
            )
            scan_task = progress.add_task(
                "",
                total=100,
                completed=0,
                visible=False,
                detail="",
            )

            def on_progress(event: dict) -> None:
                nonlocal jobs_done, jobs_failed
                kind = event.get("event")
                rel_path = event.get("rel_path", "")
                filename = _Path(rel_path).name if rel_path else ""
                duration = event.get("video_duration_sec") or 0.0

                with _lock:
                    if kind == "chunk_claimed":
                        start_ts = event["start_ts"]
                        end_ts = event["end_ts"]
                        progress.update(
                            scan_task,
                            visible=True,
                            total=100,
                            completed=0,
                            description=f"  [cyan]{filename}[/cyan]",
                            detail=f"scanning {start_ts:.0f}s – {end_ts:.0f}s",
                        )

                    elif kind == "frame_scanned":
                        _log.debug("frame_scanned: duration=%r event=%r", duration, event)
                        pts = event["pts"]
                        start_ts = event["start_ts"]
                        end_ts = event["end_ts"]
                        chunk_duration = max(end_ts - start_ts, 1.0)
                        elapsed = max(pts - start_ts, 0.0)
                        pct = min(elapsed / chunk_duration, 1.0)
                        video_pct = (pts / duration * 100) if duration > 0 else 0.0
                        progress.update(
                            scan_task,
                            completed=int(pct * 100),
                            detail=f"{elapsed:.0f}s / {chunk_duration:.0f}s  ({video_pct:.0f}% of scene)",
                        )

                    elif kind == "chunk_complete":
                        end_ts = event["end_ts"]
                        video_pct = (end_ts / duration * 100) if duration > 0 else 0.0
                        progress.update(scan_task, visible=False, detail="")
                        progress.update(
                            job_task,
                            detail=f"last chunk to {end_ts:.0f}s  ({video_pct:.0f}% of video)",
                        )

            worker = VideoIndexWorker(
                client=client,
                once=once,
                library_id=library_id,
                progress_callback=on_progress,
                artifact_store=artifact_store,
                suppress_base_progress=True,
                output_mode=output,
            )
            worker.run()

        console.print(f"Done: {jobs_done:,} succeeded, {jobs_failed:,} failed")


@worker_app.command("video-vision")
def worker_video_vision(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    once: Annotated[bool, typer.Option("--once", help="Process queue until empty then exit.")] = False,
    path: Annotated[str | None, typer.Option("--path", "-p", help="Optional subpath to scope jobs.")] = None,
    output: Annotated[
        str,
        typer.Option("--output", help="Output mode: human (default) or jsonl for structured events."),
    ] = "human",
) -> None:
    """Run the video vision worker (AI scene description for video assets)."""
    import threading
    from pathlib import Path as _Path
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
    from src.storage.artifact_store import RemoteArtifactStore
    from src.workers.video_vision_worker import VideoVisionWorker

    client = LumiverbClient()
    library_id = _resolve_library_id(client, library)
    path_prefix = normalize_path_prefix(path) if path else None
    console = Console()
    _lock = threading.Lock()
    artifact_store = RemoteArtifactStore(client=client)

    if output == "jsonl":
        worker = VideoVisionWorker(
            client=client,
            once=once,
            library_id=library_id,
            path_prefix=path_prefix,
            progress_callback=None,
            artifact_store=artifact_store,
            suppress_base_progress=True,
            output_mode=output,
        )
        worker.run()
    else:
        with Progress(
            SpinnerColumn(),
            BarColumn(),
            TextColumn("[progress.description]{task.description}"),
            TextColumn("  "),
            TextColumn("{task.fields[detail]}"),
            console=console,
            refresh_per_second=10,
        ) as progress:
            job_task = progress.add_task(
                "Processing video-vision jobs",
                total=None,
                detail="",
            )
            scene_task = progress.add_task(
                "",
                total=100,
                completed=0,
                visible=False,
                detail="",
            )

            def on_progress(event: dict) -> None:
                kind = event.get("event")
                rel_path = event.get("rel_path", "")
                filename = _Path(rel_path).name if rel_path else ""

                with _lock:
                    if kind == "job_started":
                        total = event["total_scenes"]
                        progress.update(
                            scene_task,
                            visible=True,
                            total=total,
                            completed=0,
                            description=f"  [cyan]{filename}[/cyan]",
                            detail=f"0 / {total} scenes",
                        )

                    elif kind == "scene_started":
                        scene_idx = event["scene_index"]
                        total = event["total_scenes"]
                        start_ms = event["start_ms"]
                        end_ms = event["end_ms"]
                        progress.update(
                            scene_task,
                            completed=scene_idx,
                            detail=f"{scene_idx + 1} / {total} scenes  ({start_ms // 1000}s – {end_ms // 1000}s)",
                        )

                    elif kind == "scene_complete":
                        scene_idx = event["scene_index"]
                        total = event["total_scenes"]
                        progress.update(
                            scene_task,
                            completed=scene_idx + 1,
                            detail=f"{scene_idx + 1} / {total} scenes",
                        )
                        if scene_idx + 1 >= total:
                            progress.update(scene_task, visible=False, detail="")
                            progress.update(job_task, detail=f"{filename} · {total} scenes done")

            worker = VideoVisionWorker(
                client=client,
                once=once,
                library_id=library_id,
                path_prefix=path_prefix,
                progress_callback=on_progress,
                artifact_store=artifact_store,
                suppress_base_progress=True,
                output_mode=output,
            )
            worker.run()

        console.print("Done.")


# Shell alias: function lumi-search-sync() { lumiverb worker search-sync --library "$1" --once; }
@worker_app.command("search-sync")
def worker_search_sync(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    once: Annotated[bool, typer.Option("--once", help="Process one batch then exit.")] = False,
    force_resync: Annotated[bool, typer.Option("--force-resync", help="Re-enqueue all assets before syncing.")] = False,
    path: Annotated[str | None, typer.Option("--path", "-p", help="Optional subpath to scope sync.")] = None,
    output: Annotated[
        str,
        typer.Option(
            "--output",
            help="Output mode: human (default) or jsonl for structured events.",
        ),
    ] = "human",
) -> None:
    """Run the search sync worker for a library."""
    from src.core.io_utils import normalize_path_prefix

    path_prefix = normalize_path_prefix(path)

    client = LumiverbClient()
    library_id = _resolve_library_id(client, library)

    grand_synced = 0
    grand_skipped = 0
    grand_batches = 0

    if force_resync:
        body: dict[str, object] = {"library_id": library_id}
        if path_prefix:
            body["path_prefix"] = path_prefix
        resync_resp = client.post("/v1/search-sync/resync", json=body)
        resync_resp.raise_for_status()
        n = resync_resp.json().get("enqueued", 0)
        if n:
            console.print(f"Re-enqueued {n:,} assets for resync.")

    # Check pending count via API
    params: dict[str, object] = {"library_id": library_id}
    if path_prefix:
        params["path_prefix"] = path_prefix
    pending_resp = client.get("/v1/search-sync/pending", params=params)
    pending = pending_resp.json().get("count", 0)

    if pending == 0 and once:
        console.print(f"No pending items in search_sync_queue for {library}.")
    else:
        # Process batches via API until queue is drained (or once if --once)
        while True:
            batch_body: dict[str, object] = {"library_id": library_id}
            if path_prefix:
                batch_body["path_prefix"] = path_prefix
            resp = client.post("/v1/search-sync/process-batch", json=batch_body)
            data = resp.json()
            if not data.get("processed", False):
                break
            grand_synced += int(data.get("synced", 0))
            grand_skipped += int(data.get("skipped", 0))
            grand_batches += 1
            if once:
                break

    table = Table(show_header=True)
    table.add_column("Metric", style="dim")
    table.add_column("Count", justify="right")
    table.add_row("Synced", str(grand_synced))
    table.add_row("Skipped", str(grand_skipped))
    table.add_row("Batches", str(grand_batches))
    console.print(table)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


JOB_TYPE_DISPLAY: dict[str, str] = {
    "proxy": "Proxy",
    "exif": "EXIF",
    "ai_vision": "Vision (AI)",
    "search_sync": "Search Sync",
    "embed": "Embeddings",
    "video-preview": "Video Preview",
}

JOB_TYPE_ALIASES: dict[str, str] = {
    "vision": "ai_vision",
    "video": "video-index",
}

STAGE_ORDER: list[str] = [
    "proxy",
    "exif",
    "ai_vision",
    "search_sync",
    "embed",
    "video-index",
    "video-vision",
    "video-preview",
]


def _resolve_job_type(job_type: str) -> str:
    """Resolve user-friendly aliases (e.g. vision) to actual job_type (ai_vision)."""
    return JOB_TYPE_ALIASES.get(job_type.lower(), job_type)


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


class MediaType(str, Enum):
    image = "image"
    video = "video"
    all = "all"


@pipeline_app.command("run")
def pipeline_run(
    library: Annotated[str | None, typer.Option("--library", "-l", help="Library name. Omit to run across all libraries.")] = None,
    media_type: Annotated[
        MediaType,
        typer.Option("--media-type", help="Run image, video, or all stages."),
    ] = MediaType.all,
    path: Annotated[
        str | None,
        typer.Option("--path", "-p", help="Optional subpath to scope scan and workers (single-library mode only)."),
    ] = None,
    once: Annotated[bool, typer.Option("--once", help="Run until queues are empty then exit.")] = False,
    skip_scan: Annotated[bool, typer.Option("--skip-scan", help="Skip initial scan.")] = False,
    force: Annotated[bool, typer.Option("--force", help="Force acquire lock (release existing holder).")] = False,
    retry_failures: Annotated[bool, typer.Option("--retry-failures", help="After each scan, re-enqueue failed (non-blocked) jobs for all stages.")] = False,
    interval: Annotated[int, typer.Option("--interval", help="Seconds between poll cycles in continuous mode.")] = 60,
    lock_timeout: Annotated[
        int,
        typer.Option("--lock-timeout", help="Minutes after which a stale lock can be taken."),
    ] = 5,
    log_file: Annotated[
        str | None,
        typer.Option(
            "--log-file",
            help="File to write pipeline logs to. Defaults to /tmp/pipeline.<utc_ms>.log.",
        ),
    ] = None,
) -> None:
    """Run the pipeline supervisor: acquire lock, optional scan, then poll status and run workers with a live dashboard."""
    from src.cli.pipeline_dashboard import PipelineDashboard
    from src.workers.pipeline_supervisor import LockHeartbeatClient, PathUnreachableError, PipelineSupervisor

    if path and library is None:
        console.print("[red]--path cannot be used in tenant-wide mode; specify --library to use --path[/red]")
        raise typer.Exit(1)

    client = LumiverbClient()
    ctx = client.get("/v1/tenant/context").json()
    tenant_id = ctx["tenant_id"]
    all_libraries = client.get("/v1/libraries").json()

    if library is not None:
        library_obj = _resolve_library(client, library)
        library_id = library_obj["library_id"]
        library_name = library_obj.get("name", library)
        path_prefix = normalize_path_prefix(path) if path else None
        supervisor_libraries = None
        dashboard_name = library_name
    else:
        library_id = ""
        library_name = ""
        path_prefix = None
        supervisor_libraries = [
            {"library_id": lib["library_id"], "library_name": lib.get("name", ""), "root_path": lib.get("root_path", "")}
            for lib in all_libraries
        ]
        dashboard_name = "(all libraries)"

    if log_file is not None:
        log_path = Path(log_file)
    else:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        log_path = Path(f"/tmp/pipeline.{now_ms}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    console.print(f"Writing pipeline log to {log_path}")

    # Acquire pipeline lock via API.
    acquire_resp = client.post(
        "/v1/pipeline/lock/acquire",
        json={"lock_timeout_minutes": lock_timeout, "force": force},
    )
    if acquire_resp.status_code == 409:
        detail = acquire_resp.json().get("detail", {})
        if isinstance(detail, dict):
            console.print(f"[red]{detail.get('message', 'Pipeline lock held by another process.')}[/red]")
        else:
            console.print(f"[red]{detail}[/red]")
        console.print("Use --force to override.")
        raise typer.Exit(1)
    acquire_resp.raise_for_status()
    lock_id: str = acquire_resp.json()["lock_id"]

    # Fetch total_assets from status endpoint (avoids direct DB access).
    if supervisor_libraries is not None:
        status_data = client.get("/v1/pipeline/status").json()
        total_assets = sum(lib["total_assets"] for lib in status_data.get("libraries", []))
    else:
        status_data = client.get("/v1/pipeline/status", params={"library_id": library_id}).json()
        total_assets = status_data.get("total_assets", 0)

    class _ApiLockClient:
        """Satisfies LockHeartbeatClient protocol via API calls."""

        def heartbeat(self, _tenant_id: str) -> None:
            client.post("/v1/pipeline/lock/heartbeat")

    dashboard = PipelineDashboard(dashboard_name, total_assets, str(log_path))
    supervisor = PipelineSupervisor(
        library_id=library_id,
        library_name=library_name,
        tenant_id=tenant_id,
        client=client,
        lock_repo=_ApiLockClient(),
        dashboard=dashboard,
        media_type=media_type,
        path_prefix=path_prefix,
        once=once,
        interval=interval,
        lock_timeout_minutes=lock_timeout,
        skip_scan=skip_scan,
        retry_failures=retry_failures,
        libraries=supervisor_libraries,
    )
    supervisor.set_log_file(str(log_path))
    try:
        with dashboard:
            supervisor.run()
    except PathUnreachableError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print("Interrupted.")
    finally:
        client.post("/v1/pipeline/lock/release", json={"lock_id": lock_id})

    if once and supervisor.had_worker_failure:
        raise typer.Exit(1)


@app.command("status")
def status(
    library: Annotated[str | None, typer.Option("--library", "-l", help="Library name. Omit for all libraries.")] = None,
    output: Annotated[
        str,
        typer.Option("--output", "-o", help="Output format: table (default) or json."),
    ] = "table",
) -> None:
    """Show pipeline status: asset counts by stage (proxy, EXIF, vision, search sync) with done/inflight/pending/failed breakdown, plus active worker count."""
    if output not in ("table", "json"):
        console.print("[red]--output must be one of: table, json[/red]")
        raise typer.Exit(1)

    if output == "json":
        logging.getLogger().setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)

    client = LumiverbClient()

    # Resolve library_id if a library name was given.
    library_id: str | None = None
    if library is not None:
        library_id = _resolve_library_id(client, library)

    # Fetch pipeline status from the API.
    params: dict[str, str] = {}
    if library_id is not None:
        params["library_id"] = library_id
    data = client.get("/v1/pipeline/status", params=params).json()

    if output == "json":
        print(_json.dumps(data, ensure_ascii=False))
        return

    if library is not None:
        # Single-library output
        total_assets = data.get("total_assets", 0)
        active_workers = data.get("workers", 0)
        stages = data.get("stages", [])

        console.print(f"Library: {data.get('library', library)}  ({data.get('library_id', library_id)})")
        console.print(f"Total assets: {total_assets:,}  Active workers: {active_workers}")
        console.print()

        table = Table(show_header=True)
        table.add_column("Stage", style="bold")
        table.add_column("Done", justify="right")
        table.add_column("Inflight", justify="right")
        table.add_column("Pending", justify="right")
        table.add_column("Failed", justify="right")
        table.add_column("Blocked", justify="right")

        for s in stages:
            blocked = s.get("blocked", 0)
            table.add_row(
                s.get("label", s.get("name", "")),
                f"{s['done']:,}",
                f"{s['inflight']:,}",
                f"{s['pending']:,}",
                f"{s['failed']:,}",
                f"[red]{blocked:,}[/]" if blocked else "0",
            )
        console.print(table)

        notable_stages = [
            (s["name"], s["failed"] + s.get("blocked", 0))
            for s in stages
            if (s["failed"] + s.get("blocked", 0)) > 0 and s["name"] != "search_sync"
        ]
        if notable_stages:
            worst = max(notable_stages, key=lambda x: x[1])
            hint_type = "vision" if worst[0] == "ai_vision" else worst[0]
            console.print(f"\nRun 'lumiverb failures -l {library} --job-type {hint_type}' to see failure details.")
        return

    # Tenant-wide output
    active_workers = data.get("workers", 0)
    libraries_payload = data.get("libraries", [])
    total_all = sum(lib.get("total_assets", 0) for lib in libraries_payload)
    console.print(f"Total assets (all libraries): {total_all:,}  Active workers: {active_workers}")
    console.print()
    table = Table(show_header=True)
    table.add_column("Library", style="bold")
    table.add_column("Stage", style="bold")
    table.add_column("Done", justify="right")
    table.add_column("Inflight", justify="right")
    table.add_column("Pending", justify="right")
    table.add_column("Failed", justify="right")
    table.add_column("Blocked", justify="right")
    for lib_data in libraries_payload:
        lib_name = lib_data.get("library", lib_data.get("library_id", "?"))
        for s in lib_data.get("stages", []):
            blocked = s.get("blocked", 0)
            table.add_row(
                lib_name,
                s.get("label", s.get("name", "")),
                f"{s['done']:,}",
                f"{s['inflight']:,}",
                f"{s['pending']:,}",
                f"{s['failed']:,}",
                f"[red]{blocked:,}[/]" if blocked else "0",
            )
    console.print(table)


# ---------------------------------------------------------------------------
# failures
# ---------------------------------------------------------------------------


@app.command("failures")
def failures(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    job_type: Annotated[str, typer.Option("--job-type", "-j", help="Job type (proxy, exif, ai_vision, vision, embed, ...).")],
    path: Annotated[str | None, typer.Option("--path", "-p", help="Optional path prefix to filter.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max failures to show.")] = 20,
) -> None:
    """List failed jobs with error messages. Shows most recent failure per asset. Prints retry command hint."""
    job_type = _resolve_job_type(job_type)

    client = LumiverbClient()
    library_id = _resolve_library_id(client, library)
    path_prefix = normalize_path_prefix(path)

    params: dict[str, object] = {
        "library_id": library_id,
        "job_type": job_type,
        "limit": limit,
    }
    if path_prefix:
        params["path_prefix"] = path_prefix

    data = client.get("/v1/jobs/failures", params=params).json()
    rows = data.get("rows", [])
    total_count = data.get("total_count", 0)

    def truncate(s: str, max_len: int = 60) -> str:
        if len(s) <= max_len:
            return s
        return s[: max_len - 3] + "..."

    if total_count == 0:
        console.print(f"No failed {job_type} jobs for library {library}.")
        return

    path_desc = f" under {path_prefix}" if path_prefix else ""
    showing = min(limit, len(rows))
    console.print(f"Failed {job_type} jobs for library {library}")
    console.print(f"Showing {showing} of {total_count} failures{path_desc}")
    console.print()

    table = Table(show_header=True)
    table.add_column("Path")
    table.add_column("Error")
    for r in rows:
        table.add_row(r.get("rel_path", ""), truncate(r.get("error_message", "")))
    console.print(table)

    console.print()
    console.print("To retry all:")
    console.print(f"  lumiverb enqueue -l {library} --job-type {job_type} --retry-failed")
    if path_prefix:
        console.print("To retry scoped:")
        console.print(f"  lumiverb enqueue -l {library} --job-type {job_type} --retry-failed --path {path_prefix}")


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


@app.command("scan")
def scan(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    path: Annotated[str | None, typer.Option("--path", "-p", help="Optional subpath.")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Force rescan.")] = False,
    apply_filters: Annotated[
        bool,
        typer.Option(
            "--apply-filters",
            help="After scan, trash existing assets that no longer pass the current filter set.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show which assets would be trashed by --apply-filters without making changes.",
        ),
    ] = False,
) -> None:
    """Scan a library for media files."""
    client = LumiverbClient()
    libraries = client.get("/v1/libraries").json()
    match = next((lib for lib in libraries if lib["name"] == library), None)
    if match is None:
        console.print(f"[red]Library not found: {library}[/red]")
        raise typer.Exit(1)

    result = scan_library(client, match, path_override=path, force=force)

    # Print summary table
    table = Table(title=f"Scan complete ({result.scan_id})")
    table.add_column("Metric")
    table.add_column("Count", justify="right")
    table.add_row("Discovered", str(result.files_discovered))
    table.add_row("Added", str(result.files_added))
    table.add_row("Updated", str(result.files_updated))
    table.add_row("Skipped", str(result.files_skipped))
    table.add_row("Missing", str(result.files_missing))
    console.print(table)

    if result.status == "complete" and (result.files_added > 0 or result.files_updated > 0):
        library_id = match["library_id"]
        enqueue_filter: dict = {"library_id": library_id}
        if path:
            normalised = normalize_path_prefix(path)
            if normalised:
                enqueue_filter["path_prefix"] = normalised

        for job_type in ("proxy", "exif", "video-index", "video-preview"):
            enqueue_resp = client.post(
                "/v1/jobs/enqueue",
                json={
                    "job_type": job_type,
                    "filter": enqueue_filter,
                    "force": False,
                },
            ).json()
            enqueued = enqueue_resp.get("enqueued", 0)
            console.print(f"Enqueued {enqueued:,} {job_type} jobs.")

    if result.status != "complete":
        raise typer.Exit(1)

    if apply_filters or dry_run:
        _run_apply_filters(client, match["library_id"], dry_run=dry_run)


@app.command("ingest")
def ingest(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    path: Annotated[str | None, typer.Option("--path", "-p", help="Optional subpath.")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Re-ingest already-processed assets.")] = False,
    concurrency: Annotated[int, typer.Option("--concurrency", help="Number of parallel workers.")] = 4,
    skip_vision: Annotated[bool, typer.Option("--skip-vision", help="Skip AI vision processing.")] = False,
) -> None:
    """Scan and ingest a library in one pass.

    For each image asset: generate proxy, extract EXIF, call vision AI,
    then upload everything to the server in a single atomic request.
    Videos are skipped (use the stage-based pipeline for video).
    """
    from src.cli.ingest import run_ingest

    client = LumiverbClient()
    libraries = client.get("/v1/libraries").json()
    match = next((lib for lib in libraries if lib["name"] == library), None)
    if match is None:
        console.print(f"[red]Library not found: {library}[/red]")
        raise typer.Exit(1)

    stats = run_ingest(
        client,
        match,
        concurrency=concurrency,
        skip_vision=skip_vision,
        path_override=path,
        force=force,
        console=console,
    )

    console.print(
        f"\nDone: {stats.processed:,} ingested, "
        f"{stats.failed:,} failed, "
        f"{stats.skipped:,} skipped"
    )
    if stats.failed > 0:
        raise typer.Exit(1)


def _run_apply_filters(client: LumiverbClient, library_id: str, *, dry_run: bool) -> None:
    """Apply current library filters to existing assets, trashing those that no longer match."""
    from src.cli.progress import UnifiedProgress, UnifiedProgressSpec

    # Step 1: Load current filters
    path_filters = _load_path_filters(client, library_id)
    if not path_filters:
        console.print("No filters configured for this library. Nothing to apply.")
        return

    # Step 2: Fetch all active assets and collect candidates that fail the filter set
    spec = UnifiedProgressSpec(label="Evaluating filters", unit="assets", counters=[], total=None)
    with UnifiedProgress(console, spec) as bar:
        candidates = fetch_filter_candidates(client, library_id, path_filters)
        bar.update(completed=len(candidates))
        bar.finish()

    # Step 3: Dry run output
    if dry_run:
        if candidates:
            tbl = Table(title="Dry run: assets that would be trashed")
            tbl.add_column("Asset ID")
            tbl.add_column("Path")
            for c in candidates:
                tbl.add_row(c["asset_id"], c["rel_path"])
            console.print(tbl)
        console.print(
            f"Dry run: {len(candidates):,} asset(s) would be trashed. "
            "Run with --apply-filters to apply."
        )
        return

    # Step 4: Apply
    if not candidates:
        console.print("All existing assets pass the current filters. Nothing to trash.")
        return

    confirmed = typer.confirm(
        f"Trash {len(candidates):,} asset(s) matching current exclude filters?"
    )
    if not confirmed:
        console.print("Aborted.")
        raise typer.Exit(0)

    trashed = 0
    for i in range(0, len(candidates), APPLY_FILTERS_BATCH_SIZE):
        batch = candidates[i : i + APPLY_FILTERS_BATCH_SIZE]
        asset_ids = [c["asset_id"] for c in batch]
        client.delete("/v1/assets", json={"asset_ids": asset_ids})
        trashed += len(batch)

    # TODO: cancel pending worker jobs for trashed assets — no dedicated endpoint exists yet;
    # workers check active_assets at claim time and will skip them.

    console.print(f"Trashed {trashed:,} asset(s).")


@app.command()
def enqueue(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    job_type: Annotated[
        str,
        typer.Option(
            "--job-type",
            "-j",
            help="Job type to enqueue (proxy, exif, ai_vision, embed, ...). Defaults to proxy.",
        ),
    ] = "proxy",
    path: Annotated[
        str | None,
        typer.Option(
            "--path",
            "-p",
            help="Path prefix to scope enqueue (non-recursive by default).",
        ),
    ] = None,
    recursive: Annotated[
        bool,
        typer.Option(
            "--recursive",
            "-r",
            help="Include assets in subdirectories (currently same behavior as non-recursive; server-side depth limiting TBD).",
        ),
    ] = False,
    asset_id: Annotated[
        str | None,
        typer.Option("--asset-id", help="Enqueue a single asset by ID."),
    ] = None,
    asset_path: Annotated[
        str | None,
        typer.Option("--asset-path", help="Enqueue a single asset by path."),
    ] = None,
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    missing_proxy: Annotated[bool, typer.Option("--missing-proxy")] = False,
    missing_thumbnail: Annotated[bool, typer.Option("--missing-thumbnail")] = False,
    force: Annotated[bool, typer.Option("--force", "-f")] = False,
    retry_failed: Annotated[bool, typer.Option("--retry-failed")] = False,
) -> None:
    """Enqueue processing jobs for a library or subset of assets."""
    client = LumiverbClient()
    libraries = client.get("/v1/libraries").json()
    match = next((l for l in libraries if l["name"] == library), None)
    if not match:
        console.print(f"[red]Library not found: {library}[/red]")
        raise typer.Exit(1)

    if force and retry_failed:
        console.print("[red]--force and --retry-failed are mutually exclusive[/red]")
        raise typer.Exit(1)

    if asset_id and asset_path:
        console.print("[red]--asset-id and --asset-path are mutually exclusive[/red]")
        raise typer.Exit(1)

    job_type = _resolve_job_type(job_type)

    filter_spec: dict = {
        "library_id": match["library_id"],
    }

    library_id = match["library_id"]

    if asset_path:
        resolved_asset_id = _resolve_asset_id(
            client,
            library_id=library_id,
            asset_id=None,
            path=asset_path,
        )
        filter_spec["asset_id"] = resolved_asset_id
    elif asset_id:
        filter_spec["asset_id"] = asset_id
    else:
        if path:
            norm = normalize_path_prefix(path)
            if norm:
                # TODO: implement non-recursive depth limiting server-side.
                # For now, both recursive and non-recursive use path_prefix.
                filter_spec["path_prefix"] = norm

        if since:
            filter_spec["mtime_after"] = since
        if until:
            filter_spec["mtime_before"] = until
        if missing_proxy:
            filter_spec["missing_proxy"] = True
        if missing_thumbnail:
            filter_spec["missing_thumbnail"] = True
        if retry_failed:
            filter_spec["retry_failed"] = True

    resp = client.post(
        "/v1/jobs/enqueue",
        json={
            "job_type": job_type,
            "filter": filter_spec,
            "force": force,
        },
    )
    data = resp.json()
    enqueued = data.get("enqueued", 0)
    console.print(f"Enqueued {enqueued:,} {job_type} jobs.")


@app.command("reset-video")
def reset_video(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt.")] = False,
    enqueue_after: Annotated[bool, typer.Option("--enqueue", help="Re-enqueue video-index jobs after reset.")] = True,
) -> None:
    """Reset the video indexing pipeline for a library.

    Deletes all scenes and index chunks, clears the indexed flag on every video
    asset, then optionally re-enqueues video-index so the pipeline reruns from
    scratch with the current code.
    """
    client = LumiverbClient()
    library_obj = _resolve_library(client, library)
    library_id = library_obj["library_id"]

    if not yes:
        console.print(
            f"[yellow]This will delete all video scenes and index chunks for library "
            f"[bold]{library}[/bold] and clear the indexed flag on all video assets.[/yellow]"
        )
        confirmed = typer.confirm("Continue?", default=False)
        if not confirmed:
            raise typer.Abort()

    resp = client.post("/v1/video/reset", params={"library_id": library_id})
    data = resp.json()
    console.print(
        f"Reset complete: [bold]{data['scenes_deleted']:,}[/bold] scenes deleted, "
        f"[bold]{data['chunks_deleted']:,}[/bold] chunks deleted, "
        f"[bold]{data['assets_reset']:,}[/bold] assets reset, "
        f"[bold]{data['scene_files_deleted']:,}[/bold] rep-frame files deleted, "
        f"Quickwit scene index {'deleted' if data['quickwit_index_deleted'] else 'not present'}."
    )

    if enqueue_after:
        enq_resp = client.post(
            "/v1/jobs/enqueue",
            json={
                "job_type": "video-index",
                "filter": {"library_id": library_id},
                "force": False,
            },
        )
        enqueued = enq_resp.json().get("enqueued", 0)
        console.print(f"Enqueued [bold]{enqueued:,}[/bold] video-index jobs.")


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
    asset_id: Annotated[
        str | None,
        typer.Option("--asset-id", help="Asset ID to find similar assets for."),
    ] = None,
    path: Annotated[
        str | None,
        typer.Option(
            "--path",
            "-p",
            help="Relative path to asset file within the library.",
        ),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max similar assets to return.")] = 10,
    offset: Annotated[int, typer.Option("--offset", help="Start offset for pagination.")] = 0,
    output: Annotated[str, typer.Option("--output", "-o", help="Output format: table, json, text")] = "table",
) -> None:
    """Find visually similar assets by vector similarity (default: 10 results)."""
    if output not in ("table", "json", "text"):
        console.print("[red]--output must be one of: table, json, text[/red]")
        raise typer.Exit(1)

    client = LumiverbClient()
    library_id = _resolve_library_id(client, library)
    resolved_asset_id = _resolve_asset_id(
        client,
        library_id=library_id,
        asset_id=asset_id,
        path=path,
    )

    resp = client.get(
        "/v1/similar",
        params={
            "asset_id": resolved_asset_id,
            "library_id": library_id,
            "limit": limit,
            "offset": offset,
        },
    )
    data = resp.json()
    hits = data.get("hits", [])
    embedding_available = data.get("embedding_available", False)

    if not embedding_available and not hits:
        console.print("No similar assets (source asset has no embeddings).")
        return

    if not hits:
        console.print("No similar assets.")
        return

    if output == "json":
        import sys as _sys
        _sys.stdout.write(_json.dumps(data, indent=2))
        _sys.stdout.write("\n")

    elif output == "text":
        for hit in hits:
            console.print(hit["rel_path"])

    else:  # table (default)
        table = Table(show_header=True, show_lines=False)
        table.add_column("Path", style="cyan", no_wrap=False)
        table.add_column("Distance", justify="right", style="dim")
        table.add_column("Asset ID", style="dim")
        for hit in hits:
            table.add_row(
                hit["rel_path"],
                f"{hit.get('distance', 0.0):.4f}",
                hit.get("asset_id", ""),
            )
        console.print(table)
        n = len(hits)
        console.print(f"[dim]{n} similar asset(s)[/dim]")


@app.command("similar-image")
def similar_image(
    image_path: Annotated[
        Path,
        typer.Option(
            "--image-path",
            "-i",
            help="Path to query image",
        ),
    ],
    library: Annotated[str, typer.Option("--library", "-l")] = "",
    limit: Annotated[int, typer.Option("--limit")] = 20,
    offset: Annotated[int, typer.Option("--offset")] = 0,
    output: Annotated[str, typer.Option("--output")] = "table",
    from_ts: Annotated[float | None, typer.Option("--from-ts")] = None,
    to_ts: Annotated[float | None, typer.Option("--to-ts")] = None,
    asset_types: Annotated[str | None, typer.Option("--asset-types")] = None,
    camera_make: Annotated[list[str] | None, typer.Option("--camera-make")] = None,
    camera_model: Annotated[list[str] | None, typer.Option("--camera-model")] = None,
) -> None:
    """Find visually similar assets by uploading a query image."""
    import base64
    import io
    from PIL import Image as PILImage

    PROXY_LONG_EDGE = 2048

    client = LumiverbClient()
    libraries = client.get("/v1/libraries").json()
    match = next((l for l in libraries if l.get("name") == library), None)
    if not match:
        console.print(f"[red]Library not found: {library}[/red]")
        raise typer.Exit(1)

    pil_img = PILImage.open(image_path).convert("RGB")
    w, h = pil_img.size
    scale = PROXY_LONG_EDGE / max(w, h)
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
            {
                "make": makes[i] if i < len(makes) else None,
                "model": models[i] if i < len(models) else None,
            }
            for i in range(n)
            if (makes[i] if i < len(makes) else None)
            or (models[i] if i < len(models) else None)
        ]

    asset_types_list = None
    if asset_types:
        allowed = {"image", "video"}
        asset_types_list = [
            t.strip() for t in asset_types.split(",") if t.strip() in allowed
        ]
        if not asset_types_list:
            asset_types_list = None

    payload: dict = {
        "library_id": match["library_id"],
        "image_b64": image_b64,
        "limit": limit,
        "offset": offset,
    }
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

    if output == "json":
        import sys as _sys

        _sys.stdout.write(_json.dumps(data, indent=2))
        _sys.stdout.write("\n")
        return

    from rich.table import Table as _Table

    table = _Table(show_header=True, header_style="bold")
    table.add_column("Path", no_wrap=False)
    table.add_column("Distance", justify="right", width=10)
    table.add_column("Asset ID", width=28)
    for hit in hits:
        table.add_row(
            hit.get("rel_path", ""),
            f"{hit.get('distance', 0.0):.4f}",
            hit.get("asset_id", ""),
        )
    console.print(table)
    console.print(f"{total} result(s)")


def main() -> None:
    """Entry point for the lumiverb script."""
    try:
        app()
    except LumiverbAPIError:
        # Error message already printed to stderr by the client; just exit non-zero.
        sys.exit(1)


@app.command("upgrade")
def upgrade(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show which tenant upgrade steps are pending without executing them.",
        ),
    ] = False,
    step_id: Annotated[
        str | None,
        typer.Option(
            "--step",
            help="Run only a specific upgrade step ID.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Safety valve: run a step even if preceding steps are not complete. Requires confirmation prompt.",
        ),
    ] = False,
    max_steps: Annotated[
        int,
        typer.Option(
            "--max-steps",
            help="Optional cap on how many pending upgrade steps to execute (default: 0 = all).",
        ),
    ] = 0,
) -> None:
    """Run tenant-level upgrades (schema/backfill steps) idempotently."""

    from src.cli.progress import UnifiedProgress, UnifiedProgressSpec

    client = LumiverbClient()

    if not dry_run:
        maint_resp = client.get("/v1/tenant/maintenance/status")
        maint = maint_resp.json()
        if not maint.get("active"):
            console.print("[red]Maintenance mode is not active.[/red]")
            console.print("Enable it first:")
            console.print("  lumiverb admin maintenance --start --message 'Upgrading'")
            raise typer.Exit(1)

    status_resp = client.get("/v1/tenant/upgrade/status")
    status = status_resp.json()

    steps_total = int(status.get("steps_total", 0))
    if steps_total == 0:
        console.print("[dim]No upgrade steps are registered in this build.[/dim]")
        return

    pending_step_ids: list[str] = status.get("remaining_pending_step_ids", []) or []
    has_work: bool = bool(status.get("has_work", False))
    done_steps: int = int(status.get("done_steps", status.get("completed_steps", 0)) or 0)
    steps_info: list[dict] = status.get("steps", []) or []

    if dry_run:
        if step_id is not None:
            match = next((s for s in steps_info if s.get("step_id") == step_id), None)
            if match is None:
                console.print(f"[red]Unknown upgrade step: {step_id}[/red]")
                raise typer.Exit(1)
            console.print(f"Tenant upgrade dry-run for step: {step_id}")
            console.print(f"  Step status: {match.get('status')}")
            try:
                target_index = next(i for i, s in enumerate(steps_info) if s.get("step_id") == step_id)
            except StopIteration:
                target_index = -1
            pending_or_failed_preceding: list[str] = []
            if target_index > 0:
                for s in steps_info[:target_index]:
                    if s.get("status") in ("pending", "failed"):
                        pending_or_failed_preceding.append(s.get("step_id", ""))
            if pending_or_failed_preceding:
                console.print(f"  Preceding pending/failed steps: {len(pending_or_failed_preceding)}")
                for sid in pending_or_failed_preceding:
                    console.print(f"  - {sid}")
            else:
                console.print("  Preceding steps: ready")
            return

        if not has_work:
            console.print("Tenant upgrade: [green]no pending steps[/green].")
            return
        console.print(f"Tenant upgrade (dry-run): {len(pending_step_ids)} of {steps_total} steps pending.")
        for sid in pending_step_ids:
            console.print(f"  - {sid}")
        return

    if step_id is not None:
        match = next((s for s in steps_info if s.get("step_id") == step_id), None)
        if match is None:
            console.print(f"[red]Unknown upgrade step: {step_id}[/red]")
            raise typer.Exit(1)

        pending_or_failed_preceding: list[str] = []
        try:
            target_index = next(i for i, s in enumerate(steps_info) if s.get("step_id") == step_id)
        except StopIteration:
            target_index = -1
        if target_index > 0:
            for s in steps_info[:target_index]:
                if s.get("status") in ("pending", "failed"):
                    pending_or_failed_preceding.append(s.get("step_id", ""))

        if pending_or_failed_preceding and not force:
            console.print(f"[red]Refusing to run {step_id}: preceding steps are not complete.[/red]")
            for sid in pending_or_failed_preceding:
                console.print(f"  - {sid}")
            console.print("Run without --step, or re-run with --force to override (untested territory).")
            raise typer.Exit(1)

        if pending_or_failed_preceding and force:
            confirmed = typer.confirm(
                f"Run --force and execute step '{step_id}' with {len(pending_or_failed_preceding)} preceding step(s) not complete?",
                default=False,
            )
            if not confirmed:
                console.print("Aborted.")
                return

    name_by_id = {s.get("step_id", ""): s.get("display_name", s.get("step_id", "")) for s in steps_info}
    # pending_count is the denominator: skipped steps are not counted.
    pending_count = len(pending_step_ids)
    executed_steps = 0
    failed = 0

    spec = UnifiedProgressSpec(
        label="Upgrading tenant",
        unit="steps",
        counters=["done", "failed"],
        total=pending_count,
    )
    with UnifiedProgress(console, spec) as bar:
        bar.update(completed=0, done=0, failed=0)
        if step_id is not None:
            label = name_by_id.get(step_id, step_id)
            bar.update(completed=0, description=f"{label}…", done=0, failed=0)
            resp = client.post(
                "/v1/tenant/upgrade/execute",
                json={"max_steps": 1, "step_id": step_id, "force": force},
            )
            data = resp.json()
            ran_steps = data.get("ran_steps", []) or []
            executed_steps += len(ran_steps)
            failed = int(data.get("failed_steps", failed))
            has_work = bool(data.get("has_work_after", False))
            bar.update(completed=executed_steps, done=executed_steps, failed=failed)
        else:
            remaining = list(pending_step_ids)
            while has_work and (max_steps <= 0 or executed_steps < max_steps):
                current_id = remaining[0] if remaining else None
                label = name_by_id.get(current_id, current_id) if current_id else "Upgrading tenant"
                bar.update(completed=executed_steps, description=f"{label}…", done=executed_steps, failed=failed)
                resp = client.post(
                    "/v1/tenant/upgrade/execute",
                    json={"max_steps": 1},
                )
                data = resp.json()
                ran_steps = data.get("ran_steps", []) or []
                executed_steps += len(ran_steps)
                if ran_steps and remaining:
                    remaining.pop(0)
                failed = int(data.get("failed_steps", failed))
                has_work = bool(data.get("has_work_after", False))
                bar.update(completed=executed_steps, done=executed_steps, failed=failed)

    if not has_work:
        console.print(f"Tenant upgrade: [green]completed[/green].")
    else:
        if step_id is not None:
            console.print(f"Tenant upgrade: step '{step_id}' executed (or skipped).")
        else:
            console.print(f"Tenant upgrade: stopped after {executed_steps} step(s).")
