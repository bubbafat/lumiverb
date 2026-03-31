"""Unified repair command: detect and fix missing pipeline outputs."""

from __future__ import annotations

import gc
import io
import json
import logging
import multiprocessing as mp
from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from typing import Literal

from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, MofNCompleteColumn, TimeRemainingColumn, SpinnerColumn
from rich.table import Table

from src.cli.client import LumiverbClient
from src.workers.faces.insightface_provider import InsightFaceProvider

logger = logging.getLogger(__name__)


def _silence_subprocess_stdout() -> None:
    """Redirect stdout to /dev/null in subprocess to suppress InsightFace/ONNX print noise."""
    import os
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.close(devnull)


def _drain(inflight: set[Future]) -> set[Future]:
    """Wait for at least one future to complete and return the remaining set."""
    done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
    for fut in done:
        fut.result()  # re-raise if failed
    return inflight


REPAIR_TYPES = ("embed", "vision", "faces", "search-sync", "all")
RepairType = Literal["embed", "vision", "faces", "search-sync", "all"]


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
    missing_faces: bool = False,
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
        if missing_faces:
            params["missing_faces"] = "true"
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

        image_bytes = resp.content
        resp.close()
        del resp

        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(image_bytes)).convert("RGB")
        del image_bytes
        vector = clip_provider.embed_image(img)
        img.close()
        del img

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
    finally:
        with stats.lock:
            total = stats.processed + stats.failed + stats.skipped
        if total % 10 == 0:
            gc.collect()


def _face_batch_worker(
    base_url: str,
    token: str,
    batch: list[dict],
    cache_dir: str | None = None,
) -> dict:
    """Run face detection on a batch of assets in a subprocess.

    ONNX Runtime leaks ~35MB per inference call with no fix available.
    Running in a subprocess ensures all native memory is reclaimed by
    the OS when the process exits.

    Returns {"processed": N, "failed": N, "skipped": N}.
    """
    import warnings
    warnings.filterwarnings("ignore", category=FutureWarning, module="insightface")

    from pathlib import Path

    client = LumiverbClient(base_url=base_url, token=token)
    provider = InsightFaceProvider()
    provider.ensure_loaded()

    from PIL import Image as PILImage

    cache_path = Path(cache_dir) if cache_dir else None

    import time as _time
    _batch_start = _time.perf_counter()
    _cache_hits = 0
    _downloads = 0
    _total_faces = 0

    processed = failed = skipped = 0
    errors: list[dict] = []
    for item in batch:
        asset_id = item["asset_id"]
        rel_path = item.get("rel_path", asset_id)
        try:
            # Try cache first, then fall back to server download
            image_bytes = None
            if cache_path is not None:
                cached = cache_path / asset_id
                if cached.exists():
                    image_bytes = cached.read_bytes()
                    _cache_hits += 1
            if image_bytes is None:
                _downloads += 1
                resp = client._client.get(client._url(f"/v1/assets/{asset_id}/proxy"))
                if resp.status_code != 200:
                    skipped += 1
                    errors.append({"rel_path": rel_path, "error": f"proxy HTTP {resp.status_code}"})
                    resp.close()
                    continue
                image_bytes = resp.content
                resp.close()
                del resp

            img = PILImage.open(io.BytesIO(image_bytes)).convert("RGB")
            del image_bytes
            detections = provider.detect_faces(img)
            _total_faces += len(detections)
            img.close()
            del img

            payload = [
                {
                    "bounding_box": d.bounding_box,
                    "detection_confidence": d.detection_confidence,
                    "embedding": d.embedding,
                }
                for d in detections
            ]
            del detections

            client.post(f"/v1/assets/{asset_id}/faces", json={
                "detection_model": provider.model_id,
                "detection_model_version": provider.model_version,
                "faces": payload,
            })
            del payload
            processed += 1
        except Exception as e:
            failed += 1
            errors.append({"rel_path": rel_path, "error": str(e)})

    _elapsed = _time.perf_counter() - _batch_start
    return {
        "processed": processed, "failed": failed, "skipped": skipped, "errors": errors,
        "faces_found": _total_faces, "cache_hits": _cache_hits, "downloads": _downloads,
        "elapsed": _elapsed,
    }


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
    force: bool = False,
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
    if job_type in ("faces", "all") and summary.get("missing_faces", 0) > 0:
        plan.append(("faces", summary["missing_faces"], "missing face detection"))
    if job_type in ("search-sync", "all"):
        stale = summary.get("stale_search_sync", 0)
        if force:
            plan.append(("search-sync", summary.get("total_assets", 0), "full re-index (--force)"))
        elif stale > 0:
            plan.append(("search-sync", stale, "stale search index"))

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
        ("Faces", "missing_faces", job_type in ("faces", "all")),
        ("Search sync", "stale_search_sync", job_type in ("search-sync", "all")),
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
                inflight: set[Future] = set()
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
                    inflight.add(fut)
                    if len(inflight) >= concurrency * 2:
                        inflight = _drain(inflight)
                while inflight:
                    inflight = _drain(inflight)
                pool.shutdown(wait=True)

        elif repair_type == "vision":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")
            from src.cli.ingest import run_backfill_vision
            run_backfill_vision(client, library, concurrency=concurrency, console=console)

        elif repair_type == "faces":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")

            assets = _page_missing(client, library_id, missing_faces=True)
            if not assets:
                console.print("No assets found (already repaired?).")
                continue

            # Generate proxies from local source files where possible.
            # SHA-256 mismatch means the file changed since ingest — skip
            # those assets entirely (bounding boxes would be wrong).
            from pathlib import Path
            from src.cli.proxy_cache import ProxyCache, generate_face_proxy
            root_path_str = library.get("root_path")
            root_path = Path(root_path_str).resolve() if root_path_str else None
            use_local = root_path is not None and root_path.is_dir()
            if use_local:
                from src.workers.exif_extract import compute_sha256

            proxy_cache = ProxyCache()

            # ONNX Runtime leaks ~35MB per session.run() call in its C++
            # layer — no Python-level fix (arena disable, gc, del) works.
            # Workaround: run each batch in a subprocess. When the child
            # exits, the OS reclaims all native memory. Model reload cost
            # (~2s per batch) is acceptable vs unbounded memory growth.
            from src.cli.config import load_config
            cfg = load_config()
            FACE_BATCH_SIZE = cfg.face_batch_size
            FACE_BATCH_LIMIT = cfg.face_batch_limit
            progress = _make_progress(console)
            ctx = mp.get_context("spawn")
            try:
                with progress:
                    tid = progress.add_task("Faces", total=len(assets), ok=0, fail=0)
                    pool = ctx.Pool(1, initializer=_silence_subprocess_stdout, maxtasksperchild=FACE_BATCH_LIMIT)
                    try:
                        batch_num = 0
                        for batch_start in range(0, len(assets), FACE_BATCH_SIZE):
                            batch_num += 1
                            batch = assets[batch_start:batch_start + FACE_BATCH_SIZE]

                            # Pre-generate proxy bytes from local source where possible
                            cached_count = 0
                            skipped_count = 0
                            if use_local:
                                for item in batch:
                                    asset_id = item["asset_id"]
                                    rel_path = item["rel_path"]
                                    source = root_path / rel_path
                                    if not source.is_file():
                                        continue
                                    expected_hash = item.get("sha256")
                                    if expected_hash:
                                        local_hash = compute_sha256(source)
                                        if local_hash != expected_hash:
                                            item["_skip"] = True
                                            skipped_count += 1
                                            progress.console.print(
                                                f"[yellow]faces ⚠[/yellow] {rel_path}: SHA mismatch (file changed since ingest, re-ingest needed)"
                                            )
                                            continue
                                    try:
                                        jpeg_bytes = generate_face_proxy(source)
                                        proxy_cache.put(asset_id, jpeg_bytes)
                                        cached_count += 1
                                        del jpeg_bytes
                                    except Exception:
                                        pass  # fall back to server download in worker

                            # Filter out SHA-mismatched assets
                            batch = [item for item in batch if not item.get("_skip")]
                            if not batch:
                                continue

                            logger.info("faces batch %d: %d assets (%d cached locally, %d SHA-skipped)",
                                        batch_num, len(batch), cached_count, skipped_count)

                            try:
                                result = pool.apply(
                                    _face_batch_worker,
                                    (client.base_url, client.token, batch, str(proxy_cache.path)),
                                )
                            except Exception as e:
                                console.print(f"[red]Batch failed: {e}[/red]")
                                result = {"processed": 0, "failed": len(batch), "skipped": 0, "errors": []}

                            _el = result.get("elapsed", 0)
                            _n = result["processed"] + result["failed"] + result["skipped"]
                            logger.info("faces worker: %d ok, %d fail, %d skip, %d faces, "
                                        "%d cache/%d download, %.1fs (%.0fms/img)",
                                        result["processed"], result["failed"], result["skipped"],
                                        result.get("faces_found", 0), result.get("cache_hits", 0),
                                        result.get("downloads", 0), _el, (_el / max(_n, 1)) * 1000)

                            # Remove consumed entries from cache
                            for item in batch:
                                proxy_cache.remove(item["asset_id"])

                            for err in result.get("errors", []):
                                progress.console.print(
                                    f"[red]faces ✗[/red] {err['rel_path']}: {err['error']}"
                                )

                            with stats.lock:
                                stats.processed += result["processed"]
                                stats.failed += result["failed"]
                                stats.skipped += result["skipped"]
                                ok, fail = stats.processed, stats.failed
                            batch_total = result["processed"] + result["failed"] + result["skipped"]
                            progress.advance(tid, batch_total)
                            progress.update(tid, ok=ok, fail=fail)
                    finally:
                        pool.close()
                        pool.join()
            finally:
                proxy_cache.cleanup()

        elif repair_type == "search-sync":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")
            qs = "?force=true" if force else ""
            resp = client.post(f"/v1/upkeep/search-sync{qs}")
            result = resp.json()
            synced = result.get("synced", 0)
            sync_failed = result.get("failed", 0)
            console.print(f"  Search sync: {synced} synced, {sync_failed} failed")

    console.print(f"\n[green bold]Repair complete.[/green bold] "
                  f"{stats.processed} fixed, {stats.failed} failed, {stats.skipped} skipped")
