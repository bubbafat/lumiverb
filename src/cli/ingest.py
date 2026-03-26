"""Per-asset ingest pipeline: scan + process + upload in one pass.

For each image asset: resize proxy, extract EXIF, call vision AI, then
POST everything to /v1/assets/{id}/ingest in a single request. The server
normalizes the proxy to WebP, generates the thumbnail, and stores all
metadata atomically.

Videos are skipped (they still use the stage-based pipeline).
"""

from __future__ import annotations

import io
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from rich.console import Console

from src.cli.client import LumiverbClient
from src.workers.exif_extract import compute_sha256, extract_exif, parse_gps, parse_taken_at

logger = logging.getLogger(__name__)

PROXY_LONG_EDGE = 2048
PROXY_JPEG_QUALITY = 75


def _generate_proxy_bytes(source_path: Path) -> tuple[bytes, int, int]:
    """Generate a resized JPEG proxy from a source image. Returns (bytes, width_orig, height_orig).

    Handles RAW, TIFF, and standard image formats. The server will normalize
    to WebP, so we just produce a reasonable-quality JPEG here.
    """
    import pyvips
    import numpy as np
    from PIL import Image as PILImage

    from src.core.file_extensions import RAW_EXTENSIONS

    ext = source_path.suffix.lower()
    TIFF_EXTENSIONS = {".tif", ".tiff"}

    if ext in RAW_EXTENSIONS:
        import rawpy

        # Try embedded thumb first
        try:
            with rawpy.imread(str(source_path)) as raw:
                thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                img = pyvips.Image.new_from_buffer(bytes(thumb.data), "")
                long_edge = max(img.width, img.height)
                if long_edge >= PROXY_LONG_EDGE:
                    with rawpy.imread(str(source_path)) as _raw:
                        _s = _raw.sizes
                        width_orig = _s.iwidth
                        height_orig = _s.iheight
                    proxy_img = img.thumbnail_image(
                        PROXY_LONG_EDGE, height=PROXY_LONG_EDGE,
                        size=pyvips.enums.Size.DOWN,
                    )
                    proxy_bytes = proxy_img.write_to_buffer(".jpg[Q=%d]" % PROXY_JPEG_QUALITY)
                    return proxy_bytes, width_orig, height_orig
        except Exception:
            pass

        # Full decode fallback
        with rawpy.imread(str(source_path)) as raw:
            rgb = raw.postprocess()
            _s = raw.sizes
            width_orig = _s.iwidth
            height_orig = _s.iheight
        h, w, bands = rgb.shape
        img = pyvips.Image.new_from_memory(rgb.tobytes(), w, h, bands, "uchar")
        proxy_img = img.thumbnail_image(
            PROXY_LONG_EDGE, height=PROXY_LONG_EDGE,
            size=pyvips.enums.Size.DOWN,
        )
        proxy_bytes = proxy_img.write_to_buffer(".jpg[Q=%d]" % PROXY_JPEG_QUALITY)
        return proxy_bytes, width_orig, height_orig

    elif ext in TIFF_EXTENSIONS:
        pil_img = PILImage.open(source_path)
        width_orig, height_orig = pil_img.size
        pil_img.close()
        try:
            vips_img = pyvips.Image.new_from_file(
                str(source_path),
                access=pyvips.enums.Access.SEQUENTIAL,
                fail_on=pyvips.enums.FailOn.NONE,
            )
            proxy_img = vips_img.thumbnail_image(
                PROXY_LONG_EDGE, height=PROXY_LONG_EDGE,
                size=pyvips.enums.Size.DOWN,
            ).copy_memory()
        except Exception:
            pil_img = PILImage.open(source_path)
            pil_img.thumbnail((PROXY_LONG_EDGE, PROXY_LONG_EDGE), PILImage.LANCZOS)
            pil_img = pil_img.convert("RGB")
            arr = np.asarray(pil_img, dtype=np.uint8)
            h, w, bands = arr.shape
            proxy_img = pyvips.Image.new_from_memory(arr.tobytes(), w, h, bands, "uchar")
            pil_img.close()
        proxy_bytes = proxy_img.write_to_buffer(".jpg[Q=%d]" % PROXY_JPEG_QUALITY)
        return proxy_bytes, width_orig, height_orig

    else:
        # Standard image (JPEG, PNG, WebP, etc.)
        header = pyvips.Image.new_from_file(
            str(source_path), fail_on=pyvips.enums.FailOn.NONE,
        )
        width_orig = header.width
        height_orig = header.height
        del header

        proxy_img = pyvips.Image.thumbnail(
            str(source_path), PROXY_LONG_EDGE,
            height=PROXY_LONG_EDGE,
            size=pyvips.enums.Size.DOWN,
        ).copy_memory()
        proxy_bytes = proxy_img.write_to_buffer(".jpg[Q=%d]" % PROXY_JPEG_QUALITY)
        return proxy_bytes, width_orig, height_orig


def _build_exif_payload(source_path: Path, media_type: str) -> dict:
    """Extract EXIF and build the JSON payload for the ingest endpoint."""
    from src.metadata.normalization import _parse_duration

    exif_data = extract_exif(source_path)
    sha256 = compute_sha256(source_path)
    gps_lat, gps_lon = parse_gps(exif_data)
    taken_at = parse_taken_at(exif_data)
    duration_sec = _parse_duration(exif_data, media_type == "video")

    return {
        "sha256": sha256,
        "exif": exif_data,
        "camera_make": exif_data.get("Make"),
        "camera_model": exif_data.get("Model"),
        "taken_at": taken_at.isoformat() if taken_at else None,
        "gps_lat": gps_lat,
        "gps_lon": gps_lon,
        "duration_sec": duration_sec,
    }


def _call_vision_ai(
    proxy_bytes: bytes,
    vision_model_id: str,
    vision_api_url: str,
    vision_api_key: str | None,
) -> dict | None:
    """Call the vision AI provider and return the result dict, or None if not configured."""
    if not vision_api_url or not vision_model_id:
        return None

    import tempfile

    from src.workers.captions.factory import get_caption_provider

    provider = get_caption_provider(vision_model_id, vision_api_url, vision_api_key)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(proxy_bytes)
        tmp_path = Path(tmp.name)
    try:
        result = provider.describe(tmp_path)
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


def _process_one_asset(
    *,
    client: LumiverbClient,
    asset_id: str,
    rel_path: str,
    root_path: str,
    media_type: str,
    vision_model_id: str,
    vision_api_url: str,
    vision_api_key: str | None,
    skip_vision: bool,
    console: Console,
    stats: "_IngestStats",
) -> None:
    """Process and ingest a single image asset."""
    root = Path(root_path).resolve()
    source_path = (root / rel_path).resolve()
    if not source_path.is_relative_to(root):
        logger.warning("Skipping %s: rel_path escapes library root", rel_path)
        with stats.lock:
            stats.failed += 1
        return
    if not source_path.exists():
        logger.warning("Skipping %s: file not found", rel_path)
        with stats.lock:
            stats.failed += 1
        return

    try:
        # 1. Generate proxy
        proxy_bytes, width_orig, height_orig = _generate_proxy_bytes(source_path)

        # 2. Extract EXIF
        exif_payload = _build_exif_payload(source_path, media_type)

        # 3. Call vision AI (optional)
        vision_payload = None
        if not skip_vision:
            vision_payload = _call_vision_ai(
                proxy_bytes, vision_model_id, vision_api_url, vision_api_key,
            )

        # 4. POST /v1/assets/{id}/ingest
        files = {"proxy": ("proxy.jpg", io.BytesIO(proxy_bytes), "image/jpeg")}
        data: dict[str, str] = {
            "width": str(width_orig),
            "height": str(height_orig),
            "exif": json.dumps(exif_payload),
        }
        if vision_payload is not None:
            data["vision"] = json.dumps(vision_payload)

        client.post(f"/v1/assets/{asset_id}/ingest", files=files, data=data)

        with stats.lock:
            stats.processed += 1
        print(f"ingest \u2713 {rel_path}", flush=True)

    except Exception as e:
        logger.exception("Failed to ingest %s: %s", rel_path, e)
        with stats.lock:
            stats.failed += 1
        print(f"ingest \u2717 {rel_path}: {e}", flush=True)


class _IngestStats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.processed = 0
        self.failed = 0
        self.skipped = 0


def run_ingest(
    client: LumiverbClient,
    library: dict,
    *,
    concurrency: int = 1,
    skip_vision: bool = False,
    path_override: str | None = None,
    force: bool = False,
    console: Console,
) -> _IngestStats:
    """Run the full scan + ingest pipeline for a library.

    1. Scan the library (discover/reconcile files).
    2. Fetch tenant vision config + library vision_model_id.
    3. Page through assets needing ingestion.
    4. Process each asset: proxy + EXIF + vision → POST /ingest.
    """
    from src.cli.scanner import scan_library

    library_id = library["library_id"]
    root_path = library["root_path"]
    vision_model_id = library.get("vision_model_id", "")

    # Step 1: Scan
    console.print("[bold]Scanning...[/bold]")
    scan_result = scan_library(client, library, path_override=path_override, force=force)
    console.print(
        f"Scan: {scan_result.files_discovered:,} discovered, "
        f"{scan_result.files_added:,} added, "
        f"{scan_result.files_updated:,} updated, "
        f"{scan_result.files_skipped:,} skipped"
    )

    if scan_result.status != "complete":
        console.print(f"[red]Scan failed: {scan_result.error_message}[/red]")
        stats = _IngestStats()
        return stats

    # Step 2: Get vision config from tenant
    ctx = client.get("/v1/tenant/context").json()
    vision_api_url = ctx.get("vision_api_url", "")
    vision_api_key = ctx.get("vision_api_key") or None

    if skip_vision:
        console.print("Vision AI: skipped (--skip-vision)")
    elif not vision_api_url:
        console.print("[yellow]Vision AI: not configured (no vision_api_url on tenant)[/yellow]")
        skip_vision = True
    else:
        console.print(f"Vision AI: {vision_model_id} via {vision_api_url}")

    # Step 3: Page through assets that need ingestion
    # Process assets with status=pending (newly scanned) or re-process if --force
    console.print("[bold]Ingesting...[/bold]")
    stats = _IngestStats()

    pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="ingest")
    futures = []

    after: str | None = None
    while True:
        params: dict[str, str] = {"library_id": library_id, "limit": "500"}
        if after:
            params["after"] = after
        resp = client.get("/v1/assets/page", params=params)
        if resp.status_code == 204:
            break
        assets = resp.json()
        if not assets:
            break

        for asset in assets:
            asset_id = asset["asset_id"]
            asset_status = asset.get("status", "pending")
            asset_media_type = asset.get("media_type", "")

            # Skip videos (stage-based pipeline)
            if asset_media_type == "video":
                with stats.lock:
                    stats.skipped += 1
                continue

            # Skip already-ingested unless force
            if not force and asset_status not in ("pending",):
                with stats.lock:
                    stats.skipped += 1
                continue

            fut = pool.submit(
                _process_one_asset,
                client=client,
                asset_id=asset_id,
                rel_path=asset["rel_path"],
                root_path=root_path,
                media_type=asset_media_type,
                vision_model_id=vision_model_id,
                vision_api_url=vision_api_url,
                vision_api_key=vision_api_key,
                skip_vision=skip_vision,
                console=console,
                stats=stats,
            )
            futures.append(fut)

        after = assets[-1]["asset_id"]

    # Wait for all futures
    for fut in futures:
        fut.result()
    pool.shutdown(wait=True)

    return stats
