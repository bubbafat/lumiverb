"""Shared helpers for ingest pipeline: proxy gen, EXIF, video, vision, faces.

Used by scan.py (Phase 1: file discovery + proxy upload) and repair.py
(enrichment: vision backfill, face detection). The monolithic run_ingest()
was removed in ADR-011 Phase 4 — ingest is now scan + enrich.
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, MofNCompleteColumn, TimeRemainingColumn, SpinnerColumn

from src.client.cli.client import LumiverbClient
from src.shared.file_extensions import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from src.shared.path_filter import PathFilter, is_path_included_merged
from src.client.workers.exif_extract import (
    compute_sha256,
    extract_exif,
    parse_aperture,
    parse_flash_fired,
    parse_focal_length,
    parse_gps,
    parse_iso,
    parse_lens_model,
    parse_orientation,
    parse_exposure_time_us,
    parse_taken_at,
)

logger = logging.getLogger(__name__)


def _silence_subprocess_stdout() -> None:
    """Redirect stdout to /dev/null in subprocess to suppress InsightFace/ONNX print noise."""
    import os
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.close(devnull)


from src.client.proxy.proxy_gen import PROXY_LONG_EDGE, PROXY_JPEG_QUALITY

PROXY_WEBP_QUALITY = 80
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def _jpeg_to_webp(jpeg_bytes: bytes) -> bytes:
    """Convert JPEG bytes to WebP using pyvips. Fast — no resize needed."""
    import pyvips

    img = pyvips.Image.new_from_buffer(jpeg_bytes, "")
    return img.write_to_buffer(".webp[Q=%d]" % PROXY_WEBP_QUALITY)


def _generate_proxy_bytes(source_path: Path) -> tuple[bytes, int, int]:
    """Generate a resized JPEG proxy from a source image. Returns (bytes, width_orig, height_orig)."""
    from src.client.proxy.proxy_gen import generate_proxy_bytes
    return generate_proxy_bytes(source_path)


def _build_exif_payload(source_path: Path, media_type: str) -> dict:
    """Extract EXIF and build the JSON payload for the ingest endpoint."""
    from src.client.workers.exif_extract import parse_duration

    exif_data = extract_exif(source_path)
    sha256 = compute_sha256(source_path)
    gps_lat, gps_lon = parse_gps(exif_data)
    taken_at = parse_taken_at(exif_data)
    duration_sec = parse_duration(exif_data, media_type == "video")

    return {
        "sha256": sha256,
        "exif": exif_data,
        "camera_make": exif_data.get("Make"),
        "camera_model": exif_data.get("Model"),
        "taken_at": taken_at.isoformat() if taken_at else None,
        "gps_lat": gps_lat,
        "gps_lon": gps_lon,
        "duration_sec": duration_sec,
        "iso": parse_iso(exif_data),
        "exposure_time_us": parse_exposure_time_us(exif_data),
        "aperture": parse_aperture(exif_data),
        "focal_length": parse_focal_length(exif_data, "FocalLength"),
        "focal_length_35mm": parse_focal_length(exif_data, "FocalLengthIn35mmFormat"),
        "lens_model": parse_lens_model(exif_data),
        "flash_fired": parse_flash_fired(exif_data),
        "orientation": parse_orientation(exif_data),
    }


def _call_vision_ai(
    proxy_bytes: bytes,
    vision_model_id: str,
    vision_provider: object | None,
) -> dict | None:
    """Call the vision AI provider. Returns result dict or None if not configured."""
    if vision_provider is None or not vision_model_id:
        return None

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as tmp:
        tmp.write(proxy_bytes)
        tmp_path = Path(tmp.name)
    try:
        result = vision_provider.describe(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    if not result:
        return None

    description = (result.get("description") or "").strip()
    tags = [t.strip() for t in (result.get("tags") or []) if isinstance(t, str) and t.strip()]

    return {
        "model_id": vision_model_id,
        "model_version": "1",
        "description": description,
        "tags": tags,
    }


def _resolve_vision_config(
    client: "LumiverbClient",
) -> tuple[str, str | None, str, str]:
    """Resolve vision API URL, key, model ID, and source label.

    Resolution order: client config > tenant config > auto-discover.
    Returns (vision_api_url, vision_api_key, vision_model_id, source_label).
    """
    from src.client.cli.config import load_config as _load_cli_config
    from src.client.workers.captions.model_discovery import resolve_vision_model_id

    cli_cfg = _load_cli_config()
    ctx = client.get("/v1/tenant/context").json()

    vision_api_url = cli_cfg.vision_api_url or ctx.get("vision_api_url", "")
    vision_api_key = cli_cfg.vision_api_key or ctx.get("vision_api_key") or None
    vision_source = "client config" if cli_cfg.vision_api_url else "tenant config"

    vision_model_id = ""
    if vision_api_url:
        vision_model_id = resolve_vision_model_id(
            client_model_id=cli_cfg.vision_model_id,
            tenant_model_id=ctx.get("vision_model_id", ""),
            api_url=vision_api_url,
            api_key=vision_api_key,
        )

    return vision_api_url, vision_api_key, vision_model_id, vision_source


def _detect_media_type(ext: str) -> str:
    """Return a simple media type string based on file extension."""
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return "image"


def _probe_video_dimensions(source_path: Path) -> tuple[int, int]:
    """Get the original video dimensions via ffprobe. Returns (width, height)."""
    import subprocess

    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0:s=x",
        str(source_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    # ffprobe may output multiple lines for multiple streams; take the first valid WxH pair.
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if "x" not in line:
            continue
        parts = line.split("x")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]), int(parts[1])
    raise RuntimeError(
        f"ffprobe returned no valid video dimensions for {source_path}: {result.stdout.strip()!r}"
    )


def _extract_video_poster(source_path: Path) -> tuple[bytes, int, int]:
    """Extract a poster frame (first frame) from a video. Returns (jpeg_bytes, width, height)."""
    import tempfile
    from src.client.video.clip_extractor import extract_video_frame
    import pyvips

    width_orig, height_orig = _probe_video_dimensions(source_path)

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        ok = extract_video_frame(source_path, tmp_path, timestamp=0.0)
        if not ok or not tmp_path.exists() or tmp_path.stat().st_size == 0:
            raise RuntimeError(f"Failed to extract poster frame from {source_path}")

        proxy_img = pyvips.Image.thumbnail(
            str(tmp_path), PROXY_LONG_EDGE,
            height=PROXY_LONG_EDGE,
            size=pyvips.enums.Size.DOWN,
        ).copy_memory()
        proxy_bytes = proxy_img.write_to_buffer(".jpg[Q=%d]" % PROXY_JPEG_QUALITY)
        return proxy_bytes, width_orig, height_orig
    finally:
        tmp_path.unlink(missing_ok=True)


def _generate_video_preview(source_path: Path) -> bytes:
    """Generate a 10-second MP4 preview clip. Returns preview bytes."""
    import subprocess
    import tempfile

    PREVIEW_DURATION_SEC = 10
    PREVIEW_MAX_HEIGHT = 720

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        preview_path = Path(tmp.name)

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", "0", "-i", str(source_path),
        "-t", str(PREVIEW_DURATION_SEC),
        "-vf", f"scale=-2:'min({PREVIEW_MAX_HEIGHT},ih)',format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-c:a", "aac", "-ac", "2", "-b:a", "128k",
        "-movflags", "+faststart",
        str(preview_path),
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError:
        # Retry without audio (broken audio track in some camera MOVs)
        no_audio_cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", "0", "-i", str(source_path),
            "-t", str(PREVIEW_DURATION_SEC),
            "-vf", f"scale=-2:'min({PREVIEW_MAX_HEIGHT},ih)',format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-an", "-movflags", "+faststart",
            str(preview_path),
        ]
        subprocess.run(no_audio_cmd, check=True, capture_output=True)

    try:
        if not preview_path.exists() or preview_path.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg produced no output for {source_path}")
        return preview_path.read_bytes()
    finally:
        preview_path.unlink(missing_ok=True)


def _face_batch_worker(
    base_url: str,
    token: str,
    batch: list[dict],
    cache_dir: str | None = None,
) -> dict:
    """Run face detection on a batch of assets in a subprocess.

    ONNX Runtime leaks ~35MB per inference call in its C++ layer with no
    Python-level fix. Running in a subprocess ensures all native memory is
    reclaimed by the OS when the child exits.

    Each item in *batch* must have ``asset_id`` and ``rel_path`` keys.
    If *cache_dir* is provided, proxy images are read from disk; otherwise
    they are downloaded from the server.

    Returns {"processed": N, "failed": N, "skipped": N, "errors": [...]}.
    """
    import warnings
    warnings.filterwarnings("ignore", category=FutureWarning, module="insightface")

    from pathlib import Path
    from src.client.workers.faces.insightface_provider import InsightFaceProvider
    from PIL import Image as PILImage

    client = LumiverbClient(base_url=base_url, token=token)
    provider = InsightFaceProvider()
    provider.ensure_loaded()

    import time as _time
    from concurrent.futures import ThreadPoolExecutor, Future

    cache_path = Path(cache_dir) if cache_dir else None
    _batch_start = _time.perf_counter()
    _cache_hits = 0
    _downloads = 0
    _total_faces = 0

    # Submit face results to server in background thread so detection
    # continues while the previous POST is in flight.
    submit_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="face-submit")
    pending: list[tuple[str, str, Future]] = []  # (asset_id, rel_path, future)

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
                resp = client._client.get(client._url(f"/v1/assets/{asset_id}/artifacts/proxy"))
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

    # Wait for all submissions to complete
    for asset_id, rel_path, fut in pending:
        try:
            fut.result()
            processed += 1
        except Exception as e:
            failed += 1
            errors.append({"rel_path": rel_path, "error": str(e)})
    submit_pool.shutdown(wait=True)

    _elapsed = _time.perf_counter() - _batch_start
    client.close()
    return {
        "processed": processed, "failed": failed, "skipped": skipped, "errors": errors,
        "faces_found": _total_faces, "cache_hits": _cache_hits, "downloads": _downloads,
        "elapsed": _elapsed,
    }


class _IngestStats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.processed = 0
        self.failed = 0
        self.skipped = 0
        self.removed = 0


def _drain(inflight: set[Future]) -> tuple[set[Future], set[Future]]:
    """Wait for at least one future to finish; return (done, still_pending)."""
    done, pending = _wait_first(inflight)
    for fut in done:
        fut.result()  # surfaces exceptions (already caught inside workers)
    return done, pending


def _wait_first(fs: set[Future]) -> tuple[set[Future], set[Future]]:
    """Thin wrapper so we only import wait once."""
    from concurrent.futures import wait, FIRST_COMPLETED
    return wait(fs, return_when=FIRST_COMPLETED)


def _make_progress(console: Console) -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description:<8s}"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        TextColumn("[green]{task.fields[ok]} ok[/green], [red]{task.fields[fail]} failed[/red]"),
        TimeRemainingColumn(),
        console=console,
        refresh_per_second=4,
    )


def _walk_library(
    root_path: Path,
    path_prefix: str | None = None,
    tenant_filters: list[PathFilter] | None = None,
    library_filters: list[PathFilter] | None = None,
) -> list[dict]:
    """Walk the library root and return a list of file descriptors.

    Each entry: {rel_path, file_size, file_mtime, media_type, ext}.
    Files that don't pass the merged tenant + library filters are silently skipped.
    """
    walk_root = root_path
    if path_prefix:
        walk_root = root_path / path_prefix

    if not walk_root.is_dir():
        return []

    has_filters = bool(tenant_filters or library_filters)
    t_filters = tenant_filters or []
    l_filters = library_filters or []

    results = []
    for p in sorted(walk_root.rglob("*")):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            continue

        rel_path = str(p.relative_to(root_path))

        if has_filters and not is_path_included_merged(rel_path, t_filters, l_filters):
            continue

        stat = p.stat()
        if stat.st_size == 0:
            continue

        file_mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

        results.append({
            "rel_path": rel_path,
            "file_size": stat.st_size,
            "file_mtime": file_mtime,
            "media_type": _detect_media_type(ext),
            "ext": ext,
        })

    return results


def _load_tenant_filters(client: LumiverbClient) -> list[PathFilter]:
    """Load tenant-level default filters from the API."""
    try:
        resp = client.get("/v1/tenant/filter-defaults")
        data = resp.json()
        filters: list[PathFilter] = []
        for item in data.get("includes", []):
            filters.append(PathFilter(type="include", pattern=item["pattern"]))
        for item in data.get("excludes", []):
            filters.append(PathFilter(type="exclude", pattern=item["pattern"]))
        return filters
    except Exception:
        logger.warning("Failed to load tenant filter defaults")
        return []


def _load_library_filters(client: LumiverbClient, library_id: str) -> list[PathFilter]:
    """Load library-level filters from the API."""
    try:
        resp = client.get(f"/v1/libraries/{library_id}/filters")
        data = resp.json()
        filters: list[PathFilter] = []
        for item in data.get("includes", []):
            filters.append(PathFilter(type="include", pattern=item["pattern"]))
        for item in data.get("excludes", []):
            filters.append(PathFilter(type="exclude", pattern=item["pattern"]))
        return filters
    except Exception:
        logger.warning("Failed to load library filters for %s", library_id)
        return []


# ---------------------------------------------------------------------------
# Vision backfill: add AI descriptions to assets that don't have them
# ---------------------------------------------------------------------------


def _backfill_one(
    *,
    asset_id: str,
    rel_path: str,
    vision_model_id: str,
    vision_provider: object,
    proxy_cache: "ProxyCache | None" = None,
    client: "LumiverbClient | None" = None,
) -> dict | None:
    """Read proxy from cache, call vision AI. Returns result dict or None."""
    import time as _time
    t0 = _time.perf_counter()
    proxy_bytes = proxy_cache.get(asset_id, rel_path) if proxy_cache else None
    if proxy_bytes is None and client is not None:
        resp = client.get(f"/v1/assets/{asset_id}/artifacts/proxy")
        proxy_bytes = resp.content
    t_proxy = _time.perf_counter() - t0

    if proxy_bytes is None:
        return None

    t1 = _time.perf_counter()
    vision_result = _call_vision_ai(proxy_bytes, vision_model_id, vision_provider)
    t_vision = _time.perf_counter() - t1

    logger.info("vision timings: %s — proxy=%.1fms vision=%.1fms",
                 rel_path, t_proxy * 1000, t_vision * 1000)

    if not vision_result:
        return None

    return {
        "asset_id": asset_id,
        "model_id": vision_result["model_id"],
        "model_version": vision_result["model_version"],
        "description": vision_result["description"],
        "tags": vision_result["tags"],
    }


def run_backfill_vision(
    client: LumiverbClient,
    library: dict,
    *,
    concurrency: int = 4,
    console: Console,
) -> _IngestStats:
    """Backfill AI descriptions for assets that don't have them."""
    library_id = library["library_id"]

    # Resolve vision config (client > tenant > auto-discover)
    vision_api_url, vision_api_key, vision_model_id, vision_source = _resolve_vision_config(client)

    if not vision_api_url:
        console.print("[red]Vision AI: not configured.[/red]")
        console.print("  Set it via: lumiverb config set --vision-api-url <url>")
        raise SystemExit(1)

    from src.client.workers.captions.factory import get_caption_provider
    vision_provider = get_caption_provider(vision_model_id, vision_api_url, vision_api_key)
    console.print(f"Vision AI: {vision_model_id} via {vision_api_url} ({vision_source})")

    # Page through assets missing vision
    console.print("Finding assets without AI descriptions...")
    to_backfill: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict[str, str] = {
            "library_id": library_id,
            "limit": "500",
            "missing_vision": "true",
            "sort": "asset_id",
            "dir": "asc",
        }
        if cursor:
            params["after"] = cursor
        resp = client.get("/v1/assets/page", params=params)
        data = resp.json()
        items = data.get("items", [])
        if not items:
            break
        for a in items:
            to_backfill.append(a)
        cursor = data.get("next_cursor")
        if not cursor:
            break

    stats = _IngestStats()

    if not to_backfill:
        console.print("All assets already have AI descriptions.")
        return stats

    from src.client.proxy.proxy_cache import ProxyCache
    from pathlib import Path as _Path
    root_path_str = library.get("root_path")
    root_path = _Path(root_path_str).resolve() if root_path_str else None
    if root_path and not root_path.is_dir():
        root_path = None
    proxy_cache = ProxyCache(root_path=root_path, client=client)

    BATCH_SIZE = 25
    batch_buf: list[dict] = []

    def _flush_vision_batch() -> None:
        if not batch_buf:
            return
        try:
            client.post("/v1/assets/batch-vision", json={"items": list(batch_buf)})
            logger.info("vision batch POST: %d items", len(batch_buf))
        except Exception as e:
            logger.warning("vision batch POST failed (%d items): %s", len(batch_buf), e)
            for item in batch_buf:
                try:
                    client.post(f"/v1/assets/{item['asset_id']}/vision", json=item)
                except Exception:
                    pass
        batch_buf.clear()

    def _collect(done: set[Future]) -> None:
        for f in done:
            try:
                result = f.result()
            except Exception:
                result = None
            if result is not None:
                batch_buf.append(result)
                with stats.lock:
                    stats.processed += 1
            else:
                with stats.lock:
                    stats.failed += 1
            progress.advance(tid, 1)
            with stats.lock:
                progress.update(tid, ok=stats.processed, fail=stats.failed)
            if len(batch_buf) >= BATCH_SIZE:
                _flush_vision_batch()

    progress = _make_progress(console)
    with progress:
        tid = progress.add_task("Vision", total=len(to_backfill), ok=0, fail=0)
        pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="backfill")
        inflight: set[Future] = set()
        for a in to_backfill:
            fut = pool.submit(
                _backfill_one,
                asset_id=a["asset_id"],
                rel_path=a["rel_path"],
                vision_model_id=vision_model_id,
                vision_provider=vision_provider,
                proxy_cache=proxy_cache,
                client=client,
            )
            inflight.add(fut)
            if len(inflight) >= concurrency * 2:
                done, inflight = _wait_first(inflight)
                _collect(done)

        while inflight:
            done, inflight = _wait_first(inflight)
            _collect(done)

        pool.shutdown(wait=True)
        _flush_vision_batch()

    return stats
