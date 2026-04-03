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
    client: LumiverbClient,
    asset_id: str,
    rel_path: str,
    clip_provider: object,
    proxy_cache: "ProxyCache | None" = None,
    stats: _RepairStats,
    progress: Progress,
    task_id: object,
) -> None:
    """Get proxy and generate CLIP embedding for one asset."""
    import time as _time
    try:
        t0 = _time.perf_counter()
        image_bytes = proxy_cache.get(asset_id, rel_path) if proxy_cache else None
        t_proxy = _time.perf_counter() - t0
        if image_bytes is None:
            logger.warning("No proxy for %s", rel_path)
            with stats.lock:
                stats.skipped += 1
                ok, fail = stats.processed, stats.failed
            if progress is not None:
                progress.advance(task_id, 1)
                progress.update(task_id, ok=ok, fail=fail)
            return

        from PIL import Image as PILImage
        t1 = _time.perf_counter()
        img = PILImage.open(io.BytesIO(image_bytes)).convert("RGB")
        del image_bytes
        vector = clip_provider.embed_image(img)
        img.close()
        del img
        t_embed = _time.perf_counter() - t1

        t2 = _time.perf_counter()
        client.post(f"/v1/assets/{asset_id}/embeddings", json={
            "model_id": clip_provider.model_id,
            "model_version": clip_provider.model_version,
            "vector": vector,
        })
        t_post = _time.perf_counter() - t2

        logger.info("embed timings: %s — proxy=%.1fms embed=%.1fms post=%.1fms",
                     rel_path, t_proxy * 1000, t_embed * 1000, t_post * 1000)

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
    from concurrent.futures import ThreadPoolExecutor, Future

    _batch_start = _time.perf_counter()
    _cache_hits = 0
    _downloads = 0
    _total_faces = 0

    submit_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="face-submit")
    pending: list[tuple[str, str, Future]] = []

    def _submit(asset_id: str, detection_model: str, detection_model_version: str, payload: list[dict]):
        client.post(f"/v1/assets/{asset_id}/faces", json={
            "detection_model": detection_model,
            "detection_model_version": detection_model_version,
            "faces": payload,
        })

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

            fut = submit_pool.submit(_submit, asset_id, provider.model_id, provider.model_version, payload)
            pending.append((asset_id, rel_path, fut))
        except Exception as e:
            failed += 1
            errors.append({"rel_path": rel_path, "error": str(e)})

    for asset_id, rel_path, fut in pending:
        try:
            fut.result()
            processed += 1
        except Exception as e:
            failed += 1
            errors.append({"rel_path": rel_path, "error": str(e)})
    submit_pool.shutdown(wait=True)

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
    if job_type == "redetect-faces":
        # Count ALL images, not just missing — this re-runs detection on everything
        all_images = _page_all_images(client, library_id)
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

    # Step 2: Execute repairs in logical order
    stats = _RepairStats()

    # Load concurrency config: --concurrency flag acts as max, per-type defaults are lower for GPU ops
    from src.cli.config import load_config as _load_cfg
    _cfg = _load_cfg()
    max_conc = min(concurrency, _cfg.max_concurrency)
    embed_conc = min(max_conc, concurrency)  # embed is CPU-bound (CLIP), full concurrency
    vision_conc = min(max_conc, _cfg.vision_concurrency)
    ocr_conc = min(max_conc, _cfg.ocr_concurrency)

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

            assets = _page_missing(client, library_id, missing_embeddings=True)
            if not assets:
                console.print("No assets found (already repaired?).")
                continue

            progress = _make_progress(console)
            with progress:
                tid = progress.add_task("Embeddings", total=len(assets), ok=0, fail=0)
                pool = ThreadPoolExecutor(max_workers=embed_conc, thread_name_prefix="embed")
                inflight: set[Future] = set()
                for a in assets:
                    fut = pool.submit(
                        _repair_embed_one,
                        client=client,
                        asset_id=a["asset_id"],
                        rel_path=a["rel_path"],
                        clip_provider=clip_provider,
                        proxy_cache=proxy_cache,
                        stats=stats,
                        progress=progress,
                        task_id=tid,
                    )
                    inflight.add(fut)
                    if len(inflight) >= embed_conc * 2:
                        inflight = _drain(inflight)
                while inflight:
                    inflight = _drain(inflight)
                pool.shutdown(wait=True)

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

            assets = _page_missing(client, library_id, missing_ocr=True)
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

            assets = _page_missing(client, library_id, missing_faces=True)
            if not assets:
                console.print("No assets found (already repaired?).")
                continue

            # Generate proxies from local source files where possible.
            # SHA-256 mismatch means the file changed since ingest — skip
            # those assets entirely (bounding boxes would be wrong).
            from pathlib import Path
            from src.cli.proxy_gen import generate_face_proxy
            root_path_str = library.get("root_path")
            root_path = Path(root_path_str).resolve() if root_path_str else None
            use_local = root_path is not None and root_path.is_dir()
            if use_local:
                from src.workers.exif_extract import compute_sha256

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
                                            f"[yellow]faces \u26a0[/yellow] {rel_path}: SHA mismatch (file changed since ingest, re-ingest needed)"
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

                        for err in result.get("errors", []):
                            progress.console.print(
                                f"[red]faces \u2717[/red] {err['rel_path']}: {err['error']}"
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

        elif repair_type == "redetect-faces":
            console.print(f"\n[bold]Re-detecting faces on all images ({count})[/bold]")
            console.print("[dim]Person centroids preserved — faces will auto-reassign.[/dim]")

            # all_images was already fetched during plan phase
            assets = all_images  # noqa: F821 — bound in plan phase above

            from pathlib import Path
            from src.cli.proxy_gen import generate_face_proxy
            root_path_str = library.get("root_path")
            root_path = Path(root_path_str).resolve() if root_path_str else None
            use_local = root_path is not None and root_path.is_dir()
            if use_local:
                from src.workers.exif_extract import compute_sha256

            from src.cli.config import load_config
            cfg = load_config()
            FACE_BATCH_SIZE = cfg.face_batch_size
            FACE_BATCH_LIMIT = cfg.face_batch_limit
            progress = _make_progress(console)
            ctx = mp.get_context("spawn")
            with progress:
                tid = progress.add_task("Re-detect faces", total=len(assets), ok=0, fail=0)
                pool = ctx.Pool(1, initializer=_silence_subprocess_stdout, maxtasksperchild=FACE_BATCH_LIMIT)
                try:
                    batch_num = 0
                    for batch_start in range(0, len(assets), FACE_BATCH_SIZE):
                        batch_num += 1
                        batch = assets[batch_start:batch_start + FACE_BATCH_SIZE]

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
                                            f"[yellow]faces \u26a0[/yellow] {rel_path}: SHA mismatch (file changed since ingest, re-ingest needed)"
                                        )
                                        continue
                                try:
                                    jpeg_bytes = generate_face_proxy(source)
                                    proxy_cache.put(asset_id, jpeg_bytes)
                                    cached_count += 1
                                    del jpeg_bytes
                                except Exception:
                                    pass

                        batch = [item for item in batch if not item.get("_skip")]
                        if not batch:
                            continue

                        logger.info("redetect-faces batch %d: %d assets (%d cached locally, %d SHA-skipped)",
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
                        logger.info("redetect-faces worker: %d ok, %d fail, %d skip, %d faces, "
                                    "%d cache/%d download, %.1fs (%.0fms/img)",
                                    result["processed"], result["failed"], result["skipped"],
                                    result.get("faces_found", 0), result.get("cache_hits", 0),
                                    result.get("downloads", 0), _el, (_el / max(_n, 1)) * 1000)

                        for err in result.get("errors", []):
                            progress.console.print(
                                f"[red]faces \u2717[/red] {err['rel_path']}: {err['error']}"
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

            assets = _page_missing(client, library_id, missing_video_scenes=True)
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

            assets = _page_missing(client, library_id, missing_scene_vision=True)
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

    proxy_cache.cleanup()

    console.print(f"\n[green bold]Repair complete.[/green bold] "
                  f"{stats.processed} fixed, {stats.failed} failed, {stats.skipped} skipped")
