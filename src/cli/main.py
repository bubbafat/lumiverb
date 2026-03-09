"""Typer CLI entry point: config, library create/list, scan (stub)."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from src.cli.client import LumiverbClient
from src.cli.config import load_config, save_config
from src.cli.scanner import scan_library

app = typer.Typer()
config_app = typer.Typer(help="Manage API URL and API key.")
app.add_typer(config_app, name="config")
library_app = typer.Typer(help="Create and list libraries.")
app.add_typer(library_app, name="library")

console = Console()


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@config_app.command("set")
def config_set(
    api_url: Annotated[str | None, typer.Option("--api-url")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key")] = None,
) -> None:
    """Set API URL and/or API key in ~/.lumiverb/config.json."""
    cfg = load_config()
    if api_url is not None:
        cfg.api_url = api_url.rstrip("/")
    if api_key is not None:
        cfg.api_key = api_key
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
    console.print(table)


# ---------------------------------------------------------------------------
# library
# ---------------------------------------------------------------------------


@library_app.command("create")
def library_create(
    name: Annotated[str, typer.Argument(help="Library name.")],
    path: Annotated[str, typer.Argument(help="Root path on disk.")],
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
    table.add_column("Last scan")
    for lib in libraries:
        table.add_row(
            lib.get("library_id", ""),
            lib.get("name", ""),
            lib.get("root_path", ""),
            lib.get("scan_status", ""),
            lib.get("last_scan_at") or "—",
        )
    console.print(table)


@library_app.command("set-model")
def library_set_model(
    library_id: Annotated[str, typer.Argument(help="Library ID.")],
    model: Annotated[str, typer.Argument(help="Model ID: moondream, qwen")],
) -> None:
    """Set the vision model for a library."""
    from src.models.registry import VALID_MODEL_IDS

    if model not in VALID_MODEL_IDS:
        typer.echo(f"Unknown model {model!r}. Valid: {sorted(VALID_MODEL_IDS)}")
        raise typer.Exit(1)
    client = LumiverbClient()
    r = client.patch(f"/v1/libraries/{library_id}", json={"vision_model_id": model})
    r.raise_for_status()
    typer.echo(f"Library {library_id} now uses model: {model}")


@library_app.command("delete")
def library_delete(
    name: Annotated[str, typer.Argument(help="Library name to move to trash.")],
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
# worker
# ---------------------------------------------------------------------------

worker_app = typer.Typer(help="Run background workers.")
app.add_typer(worker_app, name="worker")


def _resolve_library_id(client: object, library_name: str) -> str:
    """Resolve library name to library_id. Exits with 1 if not found."""
    libraries = client.get("/v1/libraries").json()
    match = next((l for l in libraries if l.get("name") == library_name), None)
    if match is None:
        typer.echo(f"Library not found: {library_name}", err=True)
        raise typer.Exit(1)
    return match["library_id"]


@worker_app.command("proxy")
def worker_proxy(
    once: bool = typer.Option(False, "--once", help="Process all queued jobs then exit."),
    concurrency: int = typer.Option(1, "--concurrency", help="Number of parallel workers."),
    library: str | None = typer.Option(None, "--library", help="Only process jobs for this library."),
) -> None:
    """Generate proxies and thumbnails for pending image assets."""
    from src.storage.local import LocalStorage
    from src.workers.proxy import ProxyWorker

    client = LumiverbClient()
    storage = LocalStorage()
    # tenant_id needed for storage path computation only (from lightweight context)
    ctx = client.get("/v1/tenant/context").json()
    tenant_id = ctx["tenant_id"]

    library_id: str | None = _resolve_library_id(client, library) if library else None

    worker = ProxyWorker(
        client=client,
        storage=storage,
        tenant_id=tenant_id,
        concurrency=concurrency,
        once=once,
        library_id=library_id,
    )
    worker.run()


@worker_app.command("exif")
def worker_exif(
    library: Annotated[str | None, typer.Option("--library")] = None,
    once: Annotated[bool, typer.Option("--once")] = False,
) -> None:
    """Run the EXIF metadata worker."""
    from src.workers.exif_worker import ExifWorker

    client = LumiverbClient()
    library_id = _resolve_library_id(client, library) if library else None
    worker = ExifWorker(client=client, once=once, library_id=library_id)
    worker.run()


@worker_app.command("vision")
def worker_vision(
    library: Annotated[str | None, typer.Option("--library")] = None,
    once: Annotated[bool, typer.Option("--once")] = False,
) -> None:
    """Run the AI vision worker (Moondream descriptions and tags)."""
    from src.storage.local import get_storage
    from src.workers.vision_worker import VisionWorker

    client = LumiverbClient()
    storage = get_storage()
    library_id = _resolve_library_id(client, library) if library else None
    worker = VisionWorker(
        client=client,
        storage=storage,
        once=once,
        library_id=library_id,
    )
    worker.run()


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


@app.command("scan")
def scan(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    path: Annotated[str | None, typer.Option("--path", "-p", help="Optional subpath.")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Force rescan.")] = False,
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
        for job_type in ("proxy", "exif"):
            enqueue_resp = client.post(
                "/v1/jobs/enqueue",
                json={
                    "job_type": job_type,
                    "filter": {"library_id": library_id},
                    "force": False,
                },
            ).json()
            enqueued = enqueue_resp.get("enqueued", 0)
            console.print(f"Enqueued {enqueued:,} {job_type} jobs.")

    if result.status != "complete":
        raise typer.Exit(1)


@app.command()
def enqueue(
    library: Annotated[str, typer.Argument(help="Library name")],
    job_type: Annotated[str, typer.Option("--job-type", "-j")] = "proxy",
    path: Annotated[str | None, typer.Option("--path")] = None,
    asset: Annotated[str | None, typer.Option("--asset")] = None,
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    missing_proxy: Annotated[bool, typer.Option("--missing-proxy")] = False,
    missing_thumbnail: Annotated[bool, typer.Option("--missing-thumbnail")] = False,
    force: Annotated[bool, typer.Option("--force", "-f")] = False,
) -> None:
    """Enqueue processing jobs for a library or subset of assets."""
    client = LumiverbClient()
    libraries = client.get("/v1/libraries").json()
    match = next((l for l in libraries if l["name"] == library), None)
    if not match:
        console.print(f"[red]Library not found: {library}[/red]")
        raise typer.Exit(1)

    # Build path filter: exact if it looks like a file, prefix if folder
    path_exact = None
    path_prefix = None
    if path:
        if "." in Path(path).name:
            path_exact = path
        else:
            path_prefix = path

    filter_spec: dict = {
        "library_id": match["library_id"],
    }
    if asset:
        filter_spec["asset_id"] = asset
    else:
        if path_exact:
            filter_spec["path_exact"] = path_exact
        elif path_prefix:
            filter_spec["path_prefix"] = path_prefix
        if since:
            filter_spec["mtime_after"] = since
        if until:
            filter_spec["mtime_before"] = until
        if missing_proxy:
            filter_spec["missing_proxy"] = True
        if missing_thumbnail:
            filter_spec["missing_thumbnail"] = True

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


def main() -> None:
    """Entry point for the lumiverb script."""
    app()
