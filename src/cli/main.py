"""Typer CLI entry point: config, library create/list, scan (stub)."""

from pathlib import Path
from typing import Annotated

import json as _json
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from src.cli.client import LumiverbClient
from src.cli.config import load_config, save_config
from src.cli.scanner import scan_library
from src.core.io_utils import normalize_path_prefix

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
    table.add_column("Vision Model")
    table.add_column("Last scan")
    for lib in libraries:
        table.add_row(
            lib.get("library_id", ""),
            lib.get("name", ""),
            lib.get("root_path", ""),
            lib.get("scan_status", ""),
            lib.get("vision_model_id", "moondream"),
            lib.get("last_scan_at") or "—",
        )
    console.print(table)


@library_app.command("set-model")
def library_set_model(
    library_id: Annotated[str, typer.Argument(help="Library ID.")],
    model: Annotated[
        str,
        typer.Argument(
            help='Model ID. Use "moondream" for local Moondream inference, '
            'or any OpenAI-compatible model ID (e.g. "qwen3-visioncaption-2b", "llava:13b") '
            "for remote inference via VISION_API_URL.",
        ),
    ],
) -> None:
    """Set the vision model for a library."""
    if not model.strip():
        typer.echo("Model ID cannot be empty.")
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
    library: Annotated[str | None, typer.Option("--library", "-l", help="Library name.")] = None,
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
    library: Annotated[str | None, typer.Option("--library", "-l", help="Library name.")] = None,
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
    library: Annotated[str | None, typer.Option("--library", "-l", help="Library name.")] = None,
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


# Shell alias: function lumi-search-sync() { lumiverb worker search-sync --library "$1" --once; }
@worker_app.command("search-sync")
def worker_search_sync(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    once: Annotated[bool, typer.Option("--once", help="Process one batch then exit.")] = False,
    force_resync: Annotated[bool, typer.Option("--force-resync", help="Re-enqueue all assets before syncing.")] = False,
    path: Annotated[str | None, typer.Option("--path", "-p", help="Optional subpath to scope sync.")] = None,
) -> None:
    """Run the search sync worker."""
    import time

    from src.cli.progress import UnifiedProgress, UnifiedProgressSpec
    from src.core.config import get_settings
    from src.core.database import get_tenant_session
    from src.repository.tenant import SearchSyncQueueRepository
    from src.search.quickwit_client import QuickwitClient
    from src.workers.search_sync import SearchSyncWorker
    from src.core.io_utils import normalize_path_prefix

    path_prefix = normalize_path_prefix(path)

    client = LumiverbClient()
    library_id = _resolve_library_id(client, library)
    ctx = client.get("/v1/tenant/context").json()
    tenant_id = ctx["tenant_id"]

    with get_tenant_session(tenant_id) as session:
        if force_resync:
            queue_repo = SearchSyncQueueRepository(session)
            spec = UnifiedProgressSpec(
                label="Enqueuing assets for resync",
                unit="assets",
                counters=[],
                total=None,
            )
            with UnifiedProgress(console, spec) as bar:
                def _progress(completed: int, total: int) -> None:
                    bar.update(completed=completed, total=total)

                asset_ids = queue_repo.enqueue_all_for_library(
                    library_id,
                    path_prefix=path_prefix,
                    progress_callback=_progress,
                )
            if asset_ids:
                console.print(f"Re-enqueued {len(asset_ids):,} assets for resync.")

        quickwit = QuickwitClient()
        worker = SearchSyncWorker(
            session=session,
            library_id=library_id,
            quickwit=quickwit,
            path_prefix=path_prefix,
        )
        pending = worker.pending_count()
        if pending == 0 and once:
            console.print("No pending items in search_sync_queue.")
            return

        total_synced = 0
        total_skipped = 0
        total_batches = 0
        base_synced = 0
        base_skipped = 0

        spec = UnifiedProgressSpec(
            label="Syncing search index",
            unit="assets",
            counters=["synced", "skipped"],
            total=pending,
        )
        with UnifiedProgress(console, spec) as bar:

            def _on_batch(synced: int, skipped: int, batches: int) -> None:
                bar.update(
                    completed=base_synced + base_skipped + synced + skipped,
                    synced=base_synced + synced,
                    skipped=base_skipped + skipped,
                )

            if once:
                result = worker.run_once(progress_callback=_on_batch)
                total_synced = result["synced"]
                total_skipped = result["skipped"]
                total_batches = result["batches"]
                bar.update(
                    completed=total_synced + total_skipped,
                    synced=total_synced,
                    skipped=total_skipped,
                )
            else:
                settings = get_settings()
                while True:
                    result = worker.run_once(progress_callback=_on_batch)
                    s, sk, b = result["synced"], result["skipped"], result["batches"]
                    total_synced += s
                    total_skipped += sk
                    total_batches += b
                    base_synced += s
                    base_skipped += sk
                    completed = total_synced + total_skipped
                    bar.update(
                        completed=completed,
                        synced=total_synced,
                        skipped=total_skipped,
                    )

                    if s + sk == 0:
                        # Queue empty; refresh total in case new work arrived
                        pending = worker.pending_count()
                        if pending > 0:
                            bar.update(
                                completed=completed,
                                total=completed + pending,
                                synced=total_synced,
                                skipped=total_skipped,
                            )
                        time.sleep(settings.worker_idle_poll_seconds)
                    elif completed >= pending:
                        # Caught up; refresh total for any newly enqueued work
                        new_pending = worker.pending_count()
                        pending = completed + new_pending
                        bar.update(
                            completed=completed,
                            total=pending,
                            synced=total_synced,
                            skipped=total_skipped,
                        )

        if quickwit.enabled:
            table = Table(show_header=True)
            table.add_column("Metric", style="dim")
            table.add_column("Count", justify="right")
            table.add_row("Synced", str(total_synced))
            table.add_row("Skipped", str(total_skipped))
            table.add_row("Batches", str(total_batches))
            console.print(table)
        else:
            console.print("Quickwit disabled; no assets indexed.")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


JOB_TYPE_DISPLAY: dict[str, str] = {
    "proxy": "Proxy",
    "exif": "EXIF",
    "ai_vision": "Vision (AI)",
    "search_sync": "Search Sync",
    "embed": "Embeddings",
}

JOB_TYPE_ALIASES: dict[str, str] = {
    "vision": "ai_vision",
}

STAGE_ORDER: list[str] = ["proxy", "exif", "ai_vision", "search_sync", "embed"]


def _resolve_job_type(job_type: str) -> str:
    """Resolve user-friendly aliases (e.g. vision) to actual job_type (ai_vision)."""
    return JOB_TYPE_ALIASES.get(job_type.lower(), job_type)


@app.command("status")
def status(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
) -> None:
    """Show pipeline status: asset counts by stage (proxy, EXIF, vision, search sync) with done/pending/failed breakdown."""
    from src.core.database import get_tenant_session
    from src.repository.tenant import AssetRepository, SearchSyncQueueRepository, WorkerJobRepository

    client = LumiverbClient()
    libraries = client.get("/v1/libraries").json()
    match = next((lib for lib in libraries if lib.get("name") == library), None)
    if match is None:
        console.print(f"[red]Library not found: {library}[/red]")
        raise typer.Exit(1)

    library_id = match["library_id"]
    ctx = client.get("/v1/tenant/context").json()
    tenant_id = ctx["tenant_id"]

    with get_tenant_session(tenant_id) as session:
        asset_repo = AssetRepository(session)
        job_repo = WorkerJobRepository(session)
        ssq_repo = SearchSyncQueueRepository(session)

        total_assets = asset_repo.count_by_library(library_id)
        job_rows = job_repo.pipeline_status(library_id)
        ssq_rows = ssq_repo.search_sync_pipeline_status(library_id)

    # Build pivot: stage -> {done, pending, failed}
    pivot: dict[str, dict[str, int]] = {}
    for r in job_rows:
        jt = r["job_type"]
        if jt not in pivot:
            pivot[jt] = {"done": 0, "pending": 0, "failed": 0}
        status_val = r["status"]
        count = r["count"]
        if status_val == "completed":
            pivot[jt]["done"] += count
        elif status_val in ("pending", "claimed"):
            pivot[jt]["pending"] += count
        elif status_val == "failed":
            pivot[jt]["failed"] += count

    for r in ssq_rows:
        jt = "search_sync"
        if jt not in pivot:
            pivot[jt] = {"done": 0, "pending": 0, "failed": 0}
        status_val = r["status"]
        count = r["count"]
        if status_val == "synced":
            pivot[jt]["done"] += count
        elif status_val in ("pending", "processing"):
            pivot[jt]["pending"] += count

    # Only show stages with at least one job
    stages_with_data = [s for s in STAGE_ORDER if s in pivot and (pivot[s]["done"] + pivot[s]["pending"] + pivot[s]["failed"] > 0)]

    console.print(f"Library: {library}  ({library_id})")
    console.print(f"Total assets: {total_assets:,}")
    console.print()

    table = Table(show_header=True)
    table.add_column("Stage", style="bold")
    table.add_column("Done", justify="right")
    table.add_column("Pending", justify="right")
    table.add_column("Failed", justify="right")
    for stage in stages_with_data:
        d = pivot[stage]
        display = JOB_TYPE_DISPLAY.get(stage, stage)
        table.add_row(
            display,
            f"{d['done']:,}",
            f"{d['pending']:,}",
            f"{d['failed']:,}",
        )
    console.print(table)

    any_failures = any(pivot.get(s, {}).get("failed", 0) > 0 for s in stages_with_data)
    if any_failures:
        failed_stages = [
            (s, pivot[s]["failed"])
            for s in stages_with_data
            if pivot.get(s, {}).get("failed", 0) > 0
            and s != "search_sync"
        ]
        if failed_stages:
            # Show hint for the stage with most failures
            worst = max(failed_stages, key=lambda x: x[1])
            hint_type = "vision" if worst[0] == "ai_vision" else worst[0]
        console.print(
            f"\nRun 'lumiverb failures -l {library} "
            f"--job-type {hint_type}' to see failure details."
        )


# ---------------------------------------------------------------------------
# failures
# ---------------------------------------------------------------------------


@app.command("failures")
def failures(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    job_type: Annotated[str, typer.Option("--job-type", "-j", help="Job type (proxy, exif, ai_vision, vision, ...).")],
    path: Annotated[str | None, typer.Option("--path", "-p", help="Optional path prefix to filter.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max failures to show.")] = 20,
) -> None:
    """List failed jobs with error messages. Shows most recent failure per asset. Prints retry command hint."""
    job_type = _resolve_job_type(job_type)
    from src.core.database import get_tenant_session
    from src.repository.tenant import WorkerJobRepository

    client = LumiverbClient()
    libraries = client.get("/v1/libraries").json()
    match = next((lib for lib in libraries if lib.get("name") == library), None)
    if match is None:
        console.print(f"[red]Library not found: {library}[/red]")
        raise typer.Exit(1)

    library_id = match["library_id"]
    ctx = client.get("/v1/tenant/context").json()
    tenant_id = ctx["tenant_id"]

    path_prefix = (path or "").replace("\\", "/").strip().strip("/") or None
    path_prefix = normalize_path_prefix(path)

    with get_tenant_session(tenant_id) as session:
        job_repo = WorkerJobRepository(session)
        rows, total_count = job_repo.list_failures(
            library_id=library_id,
            job_type=job_type,
            path_prefix=path_prefix,
            limit=limit,
        )

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
        table.add_row(r["rel_path"], truncate(r["error_message"]))
    console.print(table)

    path_arg = f" --path {path_prefix}" if path_prefix else ""
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

        for job_type in ("proxy", "exif"):
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


@app.command()
def enqueue(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    job_type: Annotated[str, typer.Option("--job-type", "-j")] = "proxy",
    path: Annotated[str | None, typer.Option("--path")] = None,
    asset: Annotated[str | None, typer.Option("--asset")] = None,
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

    job_type = _resolve_job_type(job_type)

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


@app.command("search")
def search(
    library: Annotated[str, typer.Option("--library", "-l", help="Library name.")],
    query: Annotated[str, typer.Argument(help="Search query.")],
    output: Annotated[str, typer.Option("--output", "-o", help="Output format: table, json, text.")] = "table",
    limit: Annotated[int, typer.Option("--limit", help="Max results. 0 = all.")] = 20,
    offset: Annotated[int, typer.Option("--offset", help="Result offset.")] = 0,
) -> None:
    """Search assets in a library by natural language query."""
    if output not in ("table", "json", "text"):
        console.print("[red]--output must be one of: table, json, text[/red]")
        raise typer.Exit(1)

    client = LumiverbClient()
    library_id = _resolve_library_id(client, library)

    if limit == 0:
        # Fetch all results by paginating until exhausted
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
                },
            )
            resp.raise_for_status()
            data = resp.json()
            source = data.get("source", "unknown")
            hits = data.get("hits", [])
            all_hits.extend(hits)
            if len(hits) < page_size:
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
            },
        )
        resp.raise_for_status()
        data = resp.json()
        hits = data.get("hits", [])
        source = data.get("source", "unknown")

    if not hits:
        console.print("[yellow]No results found.[/yellow]")
        raise typer.Exit(0)

    if output == "json":
        console.print(_json.dumps(hits, indent=2))

    elif output == "text":
        for hit in hits:
            console.print(f"[bold]{hit['rel_path']}[/bold]")
            if hit.get("description"):
                console.print(f"  {hit['description']}")
            if hit.get("tags"):
                console.print(f"  Tags: {', '.join(hit['tags'])}")
            console.print()

    else:  # table (default)
        table = Table(
            title=f"Search results for [bold]{query!r}[/bold] — {len(hits)} hit(s) via {source}",
            show_lines=False,
        )
        table.add_column("Path", style="cyan", no_wrap=False, max_width=60)
        table.add_column("Description", no_wrap=False, max_width=50)
        table.add_column("Tags", no_wrap=False, max_width=30)
        table.add_column("Score", justify="right", style="dim")
        table.add_column("Source", style="dim")

        for hit in hits:
            tags = ", ".join(hit.get("tags") or [])
            description = hit.get("description") or ""
            table.add_row(
                hit["rel_path"],
                description[:120] + "…" if len(description) > 120 else description,
                tags[:80] + "…" if len(tags) > 80 else tags,
                f"{hit.get('score', 0.0):.3f}",
                hit.get("source", ""),
            )

        console.print(table)


def main() -> None:
    """Entry point for the lumiverb script."""
    app()
