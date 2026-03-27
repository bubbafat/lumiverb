"""Unified repair command: detect and fix missing pipeline outputs."""

from __future__ import annotations

import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, MofNCompleteColumn, TimeRemainingColumn, SpinnerColumn
from rich.table import Table

from src.cli.client import LumiverbClient

logger = logging.getLogger(__name__)

REPAIR_TYPES = ("embed", "vision", "all")
RepairType = Literal["embed", "vision", "all"]


class _RepairStats:
    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.processed = 0
        self.failed = 0
        self.skipped = 0


def _make_progress(console: Console) -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        TextColumn("[green]{task.fields[ok]}[/green] ok  [red]{task.fields[fail]}[/red] fail"),
        console=console,
        transient=False,
    )


def _page_missing(
    client: LumiverbClient,
    library_id: str,
    *,
    missing_vision: bool = False,
    missing_embeddings: bool = False,
) -> list[dict]:
    """Page through assets matching the given missing filter."""
    results: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict[str, str] = {
            "library_id": library_id,
            "limit": "500",
            "sort": "asset_id",
            "dir": "asc",
        }
        if missing_vision:
            params["missing_vision"] = "true"
        if missing_embeddings:
            params["missing_embeddings"] = "true"
        if cursor:
            params["after"] = cursor
        resp = client.get("/v1/assets/page", params=params)
        data = resp.json()
        items = data.get("items", [])
        if not items:
            break
        results.extend(items)
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return results


def _repair_embed_one(
    *,
    client: LumiverbClient,
    asset_id: str,
    rel_path: str,
    clip_provider: object,
    stats: _RepairStats,
    progress: Progress,
    task_id: object,
) -> None:
    """Download proxy and generate CLIP embedding for one asset."""
    try:
        resp = client.get(f"/v1/assets/{asset_id}/proxy")
        if resp.status_code != 200:
            logger.warning("No proxy for %s (status %d)", rel_path, resp.status_code)
            with stats.lock:
                stats.skipped += 1
                ok, fail = stats.processed, stats.failed
            if progress is not None:
                progress.advance(task_id, 1)
                progress.update(task_id, ok=ok, fail=fail)
            return

        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(resp.content)).convert("RGB")
        vector = clip_provider.embed_image(img)

        client.post(f"/v1/assets/{asset_id}/embeddings", json={
            "model_id": clip_provider.model_id,
            "model_version": clip_provider.model_version,
            "vector": vector,
        })

        with stats.lock:
            stats.processed += 1
            ok, fail = stats.processed, stats.failed
        if progress is not None:
            progress.advance(task_id, 1)
            progress.update(task_id, ok=ok, fail=fail)

    except Exception as e:
        logger.exception("Failed to embed %s: %s", rel_path, e)
        with stats.lock:
            stats.failed += 1
            ok, fail = stats.processed, stats.failed
        if progress is not None:
            progress.console.print(f"[red]embed ✗[/red] {rel_path}: {e}")
            progress.advance(task_id, 1)
            progress.update(task_id, ok=ok, fail=fail)


def get_repair_summary(client: LumiverbClient, library_id: str) -> dict:
    """Fetch repair summary counts from the API."""
    resp = client.get("/v1/assets/repair-summary", params={"library_id": library_id})
    return resp.json()


def run_repair(
    client: LumiverbClient,
    library: dict,
    *,
    job_type: RepairType = "all",
    dry_run: bool = False,
    concurrency: int = 4,
    console: Console,
) -> None:
    """Detect and fix missing pipeline outputs."""
    library_id = library["library_id"]
    library_name = library["name"]

    # Step 1: Get summary
    console.print(f"[bold]Checking library: {library_name}[/bold]")
    summary = get_repair_summary(client, library_id)

    # Build repair plan
    plan: list[tuple[str, int, str]] = []  # (type, count, description)

    if job_type in ("embed", "all") and summary.get("missing_embeddings", 0) > 0:
        plan.append(("embed", summary["missing_embeddings"], "missing CLIP embeddings"))
    if job_type in ("vision", "all") and summary.get("missing_vision", 0) > 0:
        plan.append(("vision", summary["missing_vision"], "missing AI descriptions"))

    # Display summary table
    table = Table(title=f"Repair Summary — {library_name}", show_lines=False)
    table.add_column("Category", style="bold")
    table.add_column("Count", justify="right")
    table.add_column("Status")

    total = summary.get("total_assets", 0)
    table.add_row("Total assets", str(total), "")

    for label, key, needs_repair in [
        ("Proxy", "missing_proxy", job_type in ("proxy", "all")),
        ("EXIF", "missing_exif", job_type in ("exif", "all")),
        ("Embeddings", "missing_embeddings", job_type in ("embed", "all")),
        ("Vision AI", "missing_vision", job_type in ("vision", "all")),
    ]:
        count = summary.get(key, 0)
        if count == 0:
            status = "[green]✓ complete[/green]"
        elif needs_repair:
            status = f"[yellow]⚠ {count} to repair[/yellow]"
        else:
            status = f"[dim]{count} missing[/dim]"
        table.add_row(label, str(count), status)

    console.print(table)

    if not plan:
        console.print("\n[green]Nothing to repair.[/green]")
        return

    if dry_run:
        console.print("\n[dim]--dry-run: no changes made.[/dim]")
        return

    # Step 2: Execute repairs in logical order
    stats = _RepairStats()

    for repair_type, count, desc in plan:
        if repair_type == "embed":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")
            try:
                from src.workers.embeddings.clip_provider import CLIPEmbeddingProvider
                clip_provider = CLIPEmbeddingProvider()
                console.print(f"CLIP model: {clip_provider.model_version}")
            except Exception as e:
                console.print(f"[red]Cannot load CLIP model: {e}[/red]")
                continue

            assets = _page_missing(client, library_id, missing_embeddings=True)
            if not assets:
                console.print("No assets found (already repaired?).")
                continue

            progress = _make_progress(console)
            with progress:
                tid = progress.add_task("Embeddings", total=len(assets), ok=0, fail=0)
                pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="embed")
                futures = []
                for a in assets:
                    fut = pool.submit(
                        _repair_embed_one,
                        client=client,
                        asset_id=a["asset_id"],
                        rel_path=a["rel_path"],
                        clip_provider=clip_provider,
                        stats=stats,
                        progress=progress,
                        task_id=tid,
                    )
                    futures.append(fut)
                for fut in futures:
                    fut.result()
                pool.shutdown(wait=True)

        elif repair_type == "vision":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")
            from src.cli.ingest import run_backfill_vision
            run_backfill_vision(client, library, concurrency=concurrency, console=console)

    console.print(f"\n[green bold]Repair complete.[/green bold] "
                  f"{stats.processed} fixed, {stats.failed} failed, {stats.skipped} skipped")
