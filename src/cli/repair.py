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


REPAIR_TYPES = ("embed", "vision", "faces", "redetect-faces", "ocr", "video-scenes", "scene-vision", "search-sync", "all")
RepairType = Literal["embed", "vision", "faces", "redetect-faces", "ocr", "video-scenes", "scene-vision", "search-sync", "all"]


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


def _ocr_one(
    *,
    asset_id: str,
    rel_path: str,
    ocr_provider: object,
    proxy_cache: "ProxyCache | None" = None,
) -> dict | None:
    """Run OCR on one asset. Returns {"asset_id", "ocr_text"} or None on failure."""
    import time as _time
    try:
        t0 = _time.perf_counter()
        image_bytes = proxy_cache.get(asset_id, rel_path) if proxy_cache else None
        t_proxy = _time.perf_counter() - t0
        if image_bytes is None:
            return None

        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(image_bytes)
            tmp_path = Path(tmp.name)
        del image_bytes

        t1 = _time.perf_counter()
        try:
            ocr_text = ocr_provider.extract_text(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        t_ocr = _time.perf_counter() - t1

        if ocr_text:
            logger.info("OCR found: %s", ocr_text[:200])
        logger.info("ocr timings: %s — proxy=%.1fms ocr=%.1fms",
                     rel_path, t_proxy * 1000, t_ocr * 1000)
        return {"asset_id": asset_id, "ocr_text": ocr_text or ""}

    except Exception as e:
        logger.exception("Failed OCR for %s: %s", rel_path, e)
        return None




def _page_missing(
    client: LumiverbClient,
    library_id: str,
    *,
    missing_vision: bool = False,
    missing_embeddings: bool = False,
    missing_faces: bool = False,
    missing_video_scenes: bool = False,
    missing_ocr: bool = False,
    missing_scene_vision: bool = False,
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
        if missing_video_scenes:
            params["missing_video_scenes"] = "true"
        if missing_ocr:
            params["missing_ocr"] = "true"
        if missing_scene_vision:
            params["missing_scene_vision"] = "true"
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


def _page_all_images(
    client: LumiverbClient,
    library_id: str,
) -> list[dict]:
    """Page through ALL image assets in a library (for redetect-faces)."""
    results: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict[str, str] = {
            "library_id": library_id,
            "limit": "500",
            "sort": "asset_id",
            "dir": "asc",
            "media_type": "image",
        }
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
    asset_id: str,
    rel_path: str,
    clip_provider: object,
    proxy_cache: "ProxyCache | None" = None,
) -> dict | None:
    """Generate CLIP embedding for one asset. Returns result dict or None."""
    import time as _time
    t0 = _time.perf_counter()
    image_bytes = proxy_cache.get(asset_id, rel_path) if proxy_cache else None
    t_proxy = _time.perf_counter() - t0
    if image_bytes is None:
        logger.warning("No proxy for %s", rel_path)
        return None

    from PIL import Image as PILImage
    t1 = _time.perf_counter()
    img = PILImage.open(io.BytesIO(image_bytes)).convert("RGB")
    del image_bytes
    vector = clip_provider.embed_image(img)
    img.close()
    del img
    t_embed = _time.perf_counter() - t1

    logger.info("embed timings: %s — proxy=%.1fms embed=%.1fms",
                 rel_path, t_proxy * 1000, t_embed * 1000)

    return {
        "asset_id": asset_id,
        "model_id": clip_provider.model_id,
        "model_version": clip_provider.model_version,
        "vector": vector,
    }


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
    import os
    import sys
    import time as _startup_time
    import warnings
    warnings.filterwarnings("ignore", category=FutureWarning, module="insightface")

    from pathlib import Path

    _t0 = _startup_time.perf_counter()
    sys.stderr.write(f"[face-worker pid={os.getpid()}] starting, {len(batch)} assets\n")
    sys.stderr.flush()

    client = LumiverbClient(base_url=base_url, token=token)
    provider = InsightFaceProvider()
    provider.ensure_loaded()

    _t_load = _startup_time.perf_counter() - _t0
    sys.stderr.write(f"[face-worker pid={os.getpid()}] model loaded in {_t_load:.1f}s\n")
    sys.stderr.flush()

    from PIL import Image as PILImage

    cache_path = Path(cache_dir) if cache_dir else None

    import time as _time
    from concurrent.futures import ThreadPoolExecutor, Future

    _batch_start = _time.perf_counter()
    _cache_hits = 0
    _downloads = 0
    _total_faces = 0

    processed = failed = skipped = 0
    errors: list[dict] = []
    batch_items: list[dict] = []  # items for batch-faces POST

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

            batch_items.append({
                "asset_id": asset_id,
                "detection_model": provider.model_id,
                "detection_model_version": provider.model_version,
                "faces": [
                    {
                        "bounding_box": d.bounding_box,
                        "detection_confidence": d.detection_confidence,
                        "embedding": d.embedding,
                    }
                    for d in detections
                ],
            })
            del detections
        except Exception as e:
            failed += 1
            errors.append({"rel_path": rel_path, "error": str(e)})

    # Single batch POST instead of N individual requests
    if batch_items:
        try:
            resp = client.post("/v1/assets/batch-faces", json={"items": batch_items})
            result_data = resp.json()
            processed = result_data.get("processed", 0)
            skipped += result_data.get("skipped", 0)
        except Exception as e:
            # Fallback: submit individually
            sys.stderr.write(f"[face-worker] batch-faces failed ({e}), falling back to individual\n")
            for bi in batch_items:
                try:
                    client.post(f"/v1/assets/{bi['asset_id']}/faces", json={
                        "detection_model": bi["detection_model"],
                        "detection_model_version": bi["detection_model_version"],
                        "faces": bi["faces"],
                    })
                    processed += 1
                except Exception as e2:
                    failed += 1
                    errors.append({"rel_path": bi["asset_id"], "error": str(e2)})

    _elapsed = _time.perf_counter() - _batch_start
    return {
        "processed": processed, "failed": failed, "skipped": skipped, "errors": errors,
        "faces_found": _total_faces, "cache_hits": _cache_hits, "downloads": _downloads,
        "elapsed": _elapsed,
    }


def _process_face_result(
    batch_num: int,
    batch_size: int,
    ar: "mp.pool.AsyncResult",
    stats: _RepairStats,
    progress,
    tid,
    console,
) -> None:
    """Process a single completed face batch result."""
    try:
        result = ar.get()
    except Exception as e:
        console.print(f"[red]Batch {batch_num} failed: {e}[/red]")
        result = {"processed": 0, "failed": batch_size, "skipped": 0, "errors": []}

    _el = result.get("elapsed", 0)
    _n = result["processed"] + result["failed"] + result["skipped"]
    logger.info("faces worker: %d ok, %d fail, %d skip, %d faces, "
                "%d cache/%d download, %.1fs (%.0fms/img)",
                result["processed"], result["failed"], result["skipped"],
                result.get("faces_found", 0), result.get("cache_hits", 0),
                result.get("downloads", 0), _el, (_el / max(_n, 1)) * 1000)

    for err in result.get("errors", []):
        console.print(f"[red]faces \u2717[/red] {err['rel_path']}: {err['error']}")

    with stats.lock:
        stats.processed += result["processed"]
        stats.failed += result["failed"]
        stats.skipped += result["skipped"]
        ok, fail = stats.processed, stats.failed
    batch_total = result["processed"] + result["failed"] + result["skipped"]
    progress.advance(tid, batch_total)
    progress.update(tid, ok=ok, fail=fail)


def _collect_face_results(
    inflight: list,
    stats: _RepairStats,
    progress,
    tid,
    console,
    *,
    block: bool = False,
) -> None:
    """Collect all ready face batch results. If block=True, wait for at least one."""
    # Sweep all ready results
    collected = 0
    i = 0
    while i < len(inflight):
        batch_num, batch_size, ar = inflight[i]
        if ar.ready():
            inflight.pop(i)
            _process_face_result(batch_num, batch_size, ar, stats, progress, tid, console)
            collected += 1
        else:
            i += 1

    # If nothing was ready and we must block, wait on the oldest
    if collected == 0 and block and inflight:
        batch_num, batch_size, ar = inflight.pop(0)
        _process_face_result(batch_num, batch_size, ar, stats, progress, tid, console)


def _generate_proxy_for_item(
    item: dict,
    root_path: "Path | None",
    proxy_cache: object,
) -> dict | None:
    """Generate a proxy for a single asset item. Returns the item or None if skipped.

    Runs in a thread pool to pipeline proxy generation with face detection.
    Skips generation if the proxy is already in the persistent cache.
    """
    from pathlib import Path
    from src.cli.proxy_gen import generate_face_proxy

    asset_id = item["asset_id"]
    rel_path = item.get("rel_path", asset_id)
    expected_hash = item.get("sha256")

    # Check persistent cache: proxy exists AND source hash matches
    if proxy_cache.has(asset_id):
        sha_file = proxy_cache.path / f"{asset_id}.sha"
        if expected_hash and sha_file.exists():
            cached_hash = sha_file.read_text().strip()
            if cached_hash == expected_hash:
                return item  # cache hit — source unchanged
            # Stale — source changed, regenerate below
        elif not expected_hash:
            return item  # no hash to check, trust the cache

    if root_path is not None:
        source = root_path / rel_path
        if source.is_file():
            if expected_hash:
                from src.workers.exif_extract import compute_sha256
                local_hash = compute_sha256(source)
                if local_hash != expected_hash:
                    return None  # SHA mismatch — skip
            try:
                jpeg_bytes = generate_face_proxy(source)
                proxy_cache.put(asset_id, jpeg_bytes)
                if expected_hash:
                    (proxy_cache.path / f"{asset_id}.sha").write_text(expected_hash)
                del jpeg_bytes
            except Exception:
                pass  # fall back to server download in worker

    return item


def _run_face_pipeline(
    *,
    assets: list[dict],
    client: "LumiverbClient",
    proxy_cache: object,
    root_path: "Path | None",
    face_conc: int,
    batch_size: int,
    batch_limit: int,
    stats: _RepairStats,
    progress,
    tid,
    console,
    label: str = "faces",
) -> None:
    """Pipeline proxy generation → face detection → result collection.

    Proxy generation runs in a thread pool; face detection in a subprocess pool.
    A queue connects them: proxy threads put ready items, main thread consumes
    them into batches and dispatches to detection workers.
    """
    import queue
    import threading
    from concurrent.futures import ThreadPoolExecutor

    ctx = mp.get_context("spawn")
    proxy_threads = max(face_conc * 2, 4)  # I/O bound, can oversubscribe
    console.print(f"[dim]{label}: {face_conc} detect workers, {proxy_threads} proxy threads[/dim]")

    pool = ctx.Pool(face_conc, initializer=_silence_subprocess_stdout, maxtasksperchild=batch_limit)

    # Warm up detection workers — CoreML model compilation takes ~50s on first load
    console.print("[dim]Warming up face detection model (first load may take a minute)...[/dim]")
    _warmup = pool.apply_async(_face_batch_worker, (client.base_url, client.token, [], None))
    _warmup.get()  # blocks until model is loaded
    console.print("[dim]Model ready.[/dim]")

    proxy_pool = ThreadPoolExecutor(max_workers=proxy_threads, thread_name_prefix="proxy-gen")
    ready_q: queue.Queue = queue.Queue()
    _SENTINEL = None

    skipped = 0
    skip_lock = threading.Lock()

    def _proxy_worker(item: dict) -> None:
        nonlocal skipped
        result = _generate_proxy_for_item(item, root_path, proxy_cache)
        if result is None:
            with skip_lock:
                skipped += 1
            with stats.lock:
                stats.skipped += 1
            # Advance progress for skipped items from main thread via queue
            ready_q.put(("skip", None))
        else:
            ready_q.put(("item", result))

    def _submit_all() -> None:
        """Submit all proxy jobs, then signal done."""
        futures = [proxy_pool.submit(_proxy_worker, item) for item in assets]
        # Wait for all proxy gen to finish
        for f in futures:
            f.result()  # propagate exceptions
        ready_q.put(("done", None))

    # Start proxy generation in background thread
    feeder = threading.Thread(target=_submit_all, daemon=True)
    feeder.start()

    try:
        inflight: list[tuple[int, int, mp.pool.AsyncResult]] = []
        batch_buf: list[dict] = []
        batch_num = 0

        while True:
            # Sweep any ready detection results (non-blocking)
            if inflight:
                _collect_face_results(inflight, stats, progress, tid, console)

            # Get next item from proxy queue (short timeout so we keep sweeping)
            try:
                msg_type, item = ready_q.get(timeout=0.1)
            except queue.Empty:
                continue

            if msg_type == "done":
                break
            elif msg_type == "skip":
                progress.advance(tid, 1)
                continue

            batch_buf.append(item)

            if len(batch_buf) >= batch_size:
                batch_num += 1
                logger.info("%s batch %d: %d assets", label, batch_num, len(batch_buf))
                ar = pool.apply_async(
                    _face_batch_worker,
                    (client.base_url, client.token, batch_buf, str(proxy_cache.path)),
                )
                inflight.append((batch_num, len(batch_buf), ar))
                batch_buf = []

                # If too many inflight, block until one finishes
                while len(inflight) >= face_conc * 2:
                    _collect_face_results(inflight, stats, progress, tid, console, block=True)

        # Flush remaining partial batch
        if batch_buf:
            batch_num += 1
            logger.info("%s batch %d: %d assets (final)", label, batch_num, len(batch_buf))
            ar = pool.apply_async(
                _face_batch_worker,
                (client.base_url, client.token, batch_buf, str(proxy_cache.path)),
            )
            inflight.append((batch_num, len(batch_buf), ar))

        # Drain all remaining detection results
        while inflight:
            _collect_face_results(inflight, stats, progress, tid, console, block=True)

        feeder.join(timeout=5)

        if skipped:
            console.print(f"[dim]{skipped} assets skipped (SHA mismatch)[/dim]")
    finally:
        pool.close()
        pool.join()
        proxy_pool.shutdown(wait=False)


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
    asset_ids: list[str] | None = None,
    skip_types: set[str] | None = None,
) -> None:
    """Detect and fix missing pipeline outputs.

    If asset_ids is provided, only those assets are considered for enrichment.
    This is used by Phase 3 (ingest convergence) to pass scanned asset IDs
    directly, avoiding redundant re-paging of the entire library.

    If skip_types is provided, those enrichment types are excluded from the
    plan even when job_type="all". Used by ingest to honor --skip-vision
    and --skip-embeddings.
    """
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
    if job_type == "redetect-faces":
        # Count ALL images, not just missing — this re-runs detection on everything
        all_images = _filter(_page_all_images(client, library_id))
        if all_images:
            plan.append(("redetect-faces", len(all_images), "re-detect faces (all images)"))
    if job_type in ("ocr", "all") and summary.get("missing_ocr", 0) > 0:
        plan.append(("ocr", summary["missing_ocr"], "missing OCR text"))
    if job_type in ("video-scenes", "all") and summary.get("missing_video_scenes", 0) > 0:
        plan.append(("video-scenes", summary["missing_video_scenes"], "missing video scene detection"))
    if job_type in ("scene-vision", "all") and summary.get("missing_scene_vision", 0) > 0:
        plan.append(("scene-vision", summary["missing_scene_vision"], "missing scene vision AI"))
    if job_type in ("search-sync", "all"):
        stale = summary.get("stale_search_sync", 0)
        if force:
            plan.append(("search-sync", summary.get("total_assets", 0), "full re-index (--force)"))
        elif stale > 0:
            plan.append(("search-sync", stale, "stale search index"))

    # Apply skip_types filter (used by ingest --skip-vision / --skip-embeddings)
    if skip_types:
        plan = [(t, c, d) for t, c, d in plan if t not in skip_types]

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
        ("OCR", "missing_ocr", job_type in ("ocr", "all")),
        ("Video scenes", "missing_video_scenes", job_type in ("video-scenes", "all")),
        ("Scene vision", "missing_scene_vision", job_type in ("scene-vision", "all")),
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

    # Filter helper: when asset_ids is set, restrict to those IDs only.
    _id_set = set(asset_ids) if asset_ids else None

    def _filter(assets: list[dict]) -> list[dict]:
        return [a for a in assets if a["asset_id"] in _id_set] if _id_set else assets

    # Step 2: Execute repairs in logical order
    stats = _RepairStats()

    # Load concurrency config: --concurrency flag acts as max, per-type defaults are lower for GPU ops
    from src.cli.config import load_config as _load_cfg
    _cfg = _load_cfg()
    max_conc = min(concurrency, _cfg.max_concurrency)
    embed_conc = min(max_conc, concurrency)  # embed is CPU-bound (CLIP), full concurrency
    vision_conc = min(max_conc, _cfg.vision_concurrency)
    ocr_conc = min(max_conc, _cfg.ocr_concurrency)
    # Face detection uses subprocess isolation (ONNX memory leak). On macOS,
    # multiple workers loading CoreML models simultaneously can hang. Default
    # to 1 worker; concurrency applies to proxy generation threads instead.
    face_conc = 1

    # Resolve library root path for local proxy generation
    from pathlib import Path as _Path
    _root_path_str = library.get("root_path")
    root_path = _Path(_root_path_str).resolve() if _root_path_str else None
    if root_path and not root_path.is_dir():
        root_path = None

    # Shared proxy cache: generates from local source → server download → cached at configured size
    from src.cli.proxy_cache import ProxyCache
    proxy_cache = ProxyCache(max_edge=_cfg.proxy_max_edge, root_path=root_path, client=client)

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

            assets = _filter(_page_missing(client, library_id, missing_embeddings=True))
            if not assets:
                console.print("No assets found (already repaired?).")
                continue

            EMBED_BATCH_SIZE = 50
            embed_batch: list[dict] = []

            def _flush_embed_batch() -> None:
                if not embed_batch:
                    return
                try:
                    client.post("/v1/assets/batch-embeddings", json={"items": list(embed_batch)})
                    logger.info("embed batch POST: %d items", len(embed_batch))
                except Exception as e:
                    logger.warning("embed batch POST failed (%d items): %s", len(embed_batch), e)
                    for item in embed_batch:
                        try:
                            client.post(f"/v1/assets/{item['asset_id']}/embeddings", json=item)
                        except Exception:
                            pass
                embed_batch.clear()

            def _collect_embed(done: set[Future]) -> None:
                for f in done:
                    try:
                        result = f.result()
                    except Exception:
                        result = None
                    if result is not None:
                        embed_batch.append(result)
                        with stats.lock:
                            stats.processed += 1
                    else:
                        with stats.lock:
                            stats.failed += 1
                    progress.advance(tid, 1)
                    with stats.lock:
                        progress.update(tid, ok=stats.processed, fail=stats.failed)
                    if len(embed_batch) >= EMBED_BATCH_SIZE:
                        _flush_embed_batch()
                if (stats.processed + stats.failed) % 10 == 0:
                    gc.collect()

            progress = _make_progress(console)
            with progress:
                tid = progress.add_task("Embeddings", total=len(assets), ok=0, fail=0)
                pool = ThreadPoolExecutor(max_workers=embed_conc, thread_name_prefix="embed")
                inflight: set[Future] = set()
                for a in assets:
                    fut = pool.submit(
                        _repair_embed_one,
                        asset_id=a["asset_id"],
                        rel_path=a["rel_path"],
                        clip_provider=clip_provider,
                        proxy_cache=proxy_cache,
                    )
                    inflight.add(fut)
                    if len(inflight) >= embed_conc * 2:
                        done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                        _collect_embed(done)
                while inflight:
                    done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                    _collect_embed(done)
                pool.shutdown(wait=True)
                _flush_embed_batch()

        elif repair_type == "vision":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")
            from src.cli.ingest import run_backfill_vision
            run_backfill_vision(client, library, concurrency=vision_conc, console=console)

        elif repair_type == "ocr":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")
            from src.cli.ingest import _resolve_vision_config
            vision_api_url, vision_api_key, vision_model_id, vision_source = _resolve_vision_config(client)
            if not vision_api_url:
                console.print("[red]Vision AI: not configured.[/red]")
                continue
            from src.workers.captions.factory import get_caption_provider
            ocr_provider = get_caption_provider(vision_model_id, vision_api_url, vision_api_key)
            console.print(f"  Vision AI: {vision_model_id} via {vision_api_url} ({vision_source})")

            assets = _filter(_page_missing(client, library_id, missing_ocr=True))
            if not assets:
                console.print("No assets found (already repaired?).")
                continue

            import time as _time
            ocr_batch_size = _cfg.ocr_batch_size
            batch_buf: list[dict] = []

            def _flush_ocr_batch():
                if not batch_buf:
                    return
                t0 = _time.perf_counter()
                try:
                    client.post("/v1/assets/batch-ocr", json={"items": list(batch_buf)})
                    t_post = _time.perf_counter() - t0
                    logger.info("ocr batch POST: %d items in %.1fms", len(batch_buf), t_post * 1000)
                except Exception as e:
                    logger.warning("ocr batch POST failed (%d items): %s", len(batch_buf), e)
                    # Fallback: post individually
                    for item in batch_buf:
                        try:
                            client.post(f"/v1/assets/{item['asset_id']}/ocr", json={"ocr_text": item["ocr_text"]})
                        except Exception:
                            pass
                batch_buf.clear()

            progress = _make_progress(console)
            with progress:
                tid = progress.add_task("OCR", total=len(assets), ok=0, fail=0)
                for a in assets:
                    result = _ocr_one(
                        asset_id=a["asset_id"],
                        rel_path=a["rel_path"],
                        ocr_provider=ocr_provider,
                        proxy_cache=proxy_cache,
                    )
                    if result is not None:
                        batch_buf.append(result)
                        with stats.lock:
                            stats.processed += 1
                    else:
                        with stats.lock:
                            stats.skipped += 1

                    if len(batch_buf) >= ocr_batch_size:
                        _flush_ocr_batch()

                    with stats.lock:
                        ok, fail = stats.processed, stats.failed
                    progress.advance(tid, 1)
                    progress.update(tid, ok=ok, fail=fail)

                _flush_ocr_batch()  # flush remaining

        elif repair_type == "faces":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")

            assets = _filter(_page_missing(client, library_id, missing_faces=True))
            if not assets:
                console.print("No assets found (already repaired?).")
                continue

            from pathlib import Path
            root_path_str = library.get("root_path")
            _face_root = Path(root_path_str).resolve() if root_path_str else None
            if _face_root and not _face_root.is_dir():
                _face_root = None

            from src.cli.config import load_config
            cfg = load_config()

            progress = _make_progress(console)
            with progress:
                tid = progress.add_task("Faces", total=len(assets), ok=0, fail=0)
                _run_face_pipeline(
                    assets=assets,
                    client=client,
                    proxy_cache=proxy_cache,
                    root_path=_face_root,
                    face_conc=face_conc,
                    batch_size=cfg.face_batch_size,
                    batch_limit=cfg.face_batch_limit,
                    stats=stats,
                    progress=progress,
                    tid=tid,
                    console=console,
                    label="faces",
                )

        elif repair_type == "redetect-faces":
            console.print(f"\n[bold]Re-detecting faces on all images ({count})[/bold]")
            console.print("[dim]Person centroids preserved — faces will auto-reassign.[/dim]")

            assets = all_images  # noqa: F821 — bound in plan phase above

            from pathlib import Path
            root_path_str = library.get("root_path")
            _face_root = Path(root_path_str).resolve() if root_path_str else None
            if _face_root and not _face_root.is_dir():
                _face_root = None

            from src.cli.config import load_config
            cfg = load_config()

            progress = _make_progress(console)
            with progress:
                tid = progress.add_task("Re-detect faces", total=len(assets), ok=0, fail=0)
                _run_face_pipeline(
                    assets=assets,
                    client=client,
                    proxy_cache=proxy_cache,
                    root_path=_face_root,
                    face_conc=face_conc,
                    batch_size=cfg.face_batch_size,
                    batch_limit=cfg.face_batch_limit,
                    stats=stats,
                    progress=progress,
                    tid=tid,
                    console=console,
                    label="redetect-faces",
                )

            # Clean up dismissed people left with zero face matches
            try:
                resp = client.post("/v1/upkeep/cleanup-dismissed")
                deleted = resp.json().get("deleted", 0)
                if deleted:
                    console.print(f"[dim]Cleaned up {deleted} empty dismissed people.[/dim]")
            except Exception as e:
                logger.warning("cleanup-dismissed failed: %s", e)

        elif repair_type == "video-scenes":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")

            from pathlib import Path
            root_path_str = library.get("root_path")
            root_path = Path(root_path_str).resolve() if root_path_str else None
            if root_path is None or not root_path.is_dir():
                console.print("[red]Library root not accessible — cannot run scene detection[/red]")
                continue

            assets = _filter(_page_missing(client, library_id, missing_video_scenes=True))
            if not assets:
                console.print("No assets found (already repaired?).")
                continue

            videos = [
                {"asset_id": a["asset_id"], "rel_path": a["rel_path"], "duration_sec": a.get("duration_sec")}
                for a in assets
            ]
            indexable = [v for v in videos if v.get("duration_sec")]
            if not indexable:
                console.print("No videos with known duration found.")
                continue

            from src.cli.video_index import run_video_index
            progress = _make_progress(console)
            with progress:
                tid = progress.add_task("Scenes", total=len(indexable), ok=0, fail=0)
                run_video_index(
                    client=client,
                    root_path=root_path,
                    videos=indexable,
                    console=console,
                    progress=progress,
                    task_id=tid,
                )

        elif repair_type == "scene-vision":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")

            from pathlib import Path as _Path
            root_path_str = library.get("root_path")
            root_path = _Path(root_path_str).resolve() if root_path_str else None
            if root_path is None or not root_path.is_dir():
                console.print("[red]Library root not accessible — cannot run scene vision[/red]")
                continue

            # Resolve vision config
            from src.cli.ingest import _resolve_vision_config
            vision_api_url, vision_api_key, vision_model_id, vision_source = _resolve_vision_config(client)
            scene_vision_provider = None
            if vision_api_url and vision_model_id:
                from src.workers.captions.factory import get_caption_provider
                scene_vision_provider = get_caption_provider(vision_model_id, vision_api_url, vision_api_key)
                console.print(f"  Vision AI: {vision_model_id} via {vision_api_url} ({vision_source})")
            else:
                console.print("  Vision AI: not configured — extracting rep frames only")

            assets = _filter(_page_missing(client, library_id, missing_scene_vision=True))
            if not assets:
                console.print("No assets found (already repaired?).")
                continue

            videos = [{"asset_id": a["asset_id"], "rel_path": a["rel_path"]} for a in assets]

            from src.cli.video_index import run_video_enrich
            progress = _make_progress(console)
            with progress:
                tid = progress.add_task("Scene vision", total=len(videos), ok=0, fail=0)
                run_video_enrich(
                    client=client,
                    root_path=root_path,
                    videos=videos,
                    vision_provider=scene_vision_provider,
                    vision_model_id=vision_model_id,
                    console=console,
                    progress=progress,
                    task_id=tid,
                )

        elif repair_type == "search-sync":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")
            qs = "?force=true" if force else ""
            resp = client.post(f"/v1/upkeep/search-sync{qs}")
            result = resp.json()
            synced = result.get("synced", 0)
            sync_failed = result.get("failed", 0)
            console.print(f"  Search sync: {synced} synced, {sync_failed} failed")

    # Proxy cache is persistent (shared between scan and enrich) — do not clean up.

    console.print(f"\n[green bold]Repair complete.[/green bold] "
                  f"{stats.processed} fixed, {stats.failed} failed, {stats.skipped} skipped")
