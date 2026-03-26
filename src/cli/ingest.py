"""Per-asset ingest pipeline: discover files + process + create-on-ingest.

Walks the library root, and for each image file: resize proxy, extract
EXIF, call vision AI, then POST /v1/ingest to create the asset record and
store all data atomically. The asset only appears on the server once it's
fully populated — no partial state.

Videos are skipped (they still use the stage-based pipeline).
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from src.cli.client import LumiverbClient
from src.core.file_extensions import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from src.workers.exif_extract import compute_sha256, extract_exif, parse_gps, parse_taken_at

logger = logging.getLogger(__name__)

PROXY_LONG_EDGE = 2048
PROXY_JPEG_QUALITY = 75
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def _generate_proxy_bytes(source_path: Path) -> tuple[bytes, int, int]:
    """Generate a resized JPEG proxy from a source image. Returns (bytes, width_orig, height_orig)."""
    import pyvips
    import numpy as np
    from PIL import Image as PILImage

    from src.core.file_extensions import RAW_EXTENSIONS

    ext = source_path.suffix.lower()
    TIFF_EXTENSIONS = {".tif", ".tiff"}

    if ext in RAW_EXTENSIONS:
        import rawpy

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
    """Call the vision AI provider. Returns result dict or None if not configured."""
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


def _detect_media_type(ext: str) -> str:
    """Return a simple media type string based on file extension."""
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return "image"


def _process_and_ingest_one(
    *,
    client: LumiverbClient,
    library_id: str,
    root_path: Path,
    rel_path: str,
    file_size: int,
    file_mtime: datetime | None,
    media_type: str,
    vision_model_id: str,
    vision_api_url: str,
    vision_api_key: str | None,
    skip_vision: bool,
    stats: "_IngestStats",
) -> None:
    """Process one image file and POST /v1/ingest to create + populate atomically."""
    source_path = (root_path / rel_path).resolve()
    if not source_path.is_relative_to(root_path):
        logger.warning("Skipping %s: escapes library root", rel_path)
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

        # 4. POST /v1/ingest — create asset + ingest atomically
        files = {"proxy": ("proxy.jpg", io.BytesIO(proxy_bytes), "image/jpeg")}
        data: dict[str, str] = {
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": str(file_size),
            "media_type": media_type,
            "width": str(width_orig),
            "height": str(height_orig),
            "exif": json.dumps(exif_payload),
        }
        if file_mtime is not None:
            data["file_mtime"] = file_mtime.isoformat()
        if vision_payload is not None:
            data["vision"] = json.dumps(vision_payload)

        client.post("/v1/ingest", files=files, data=data)

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


def _walk_library(root_path: Path, path_prefix: str | None = None) -> list[dict]:
    """Walk the library root and return a list of file descriptors.

    Each entry: {rel_path, file_size, file_mtime, media_type, ext}.
    """
    walk_root = root_path
    if path_prefix:
        walk_root = root_path / path_prefix

    if not walk_root.is_dir():
        return []

    results = []
    for p in sorted(walk_root.rglob("*")):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            continue

        rel_path = str(p.relative_to(root_path))
        stat = p.stat()
        file_mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

        results.append({
            "rel_path": rel_path,
            "file_size": stat.st_size,
            "file_mtime": file_mtime,
            "media_type": _detect_media_type(ext),
            "ext": ext,
        })

    return results


def _fetch_existing_rel_paths(client: LumiverbClient, library_id: str) -> set[str]:
    """Page through all assets on the server and collect their rel_paths."""
    existing: set[str] = set()
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
        for a in assets:
            existing.add(a["rel_path"])
        after = assets[-1]["asset_id"]
    return existing


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
    """Discover files and ingest each image atomically.

    1. Walk the filesystem to find media files.
    2. Fetch existing assets from the server (skip unless --force).
    3. Fetch tenant vision config.
    4. For each new/updated file: proxy + EXIF + vision → POST /v1/ingest.
    """
    library_id = library["library_id"]
    root_path = Path(library["root_path"]).resolve()
    vision_model_id = library.get("vision_model_id", "")

    # Step 1: Discover files
    console.print("[bold]Discovering files...[/bold]")
    local_files = _walk_library(root_path, path_override)
    console.print(f"Found {len(local_files):,} media files")

    if not local_files:
        return _IngestStats()

    # Step 2: Fetch existing assets to skip already-ingested
    stats = _IngestStats()
    if not force:
        console.print("Checking server for existing assets...")
        existing = _fetch_existing_rel_paths(client, library_id)
        console.print(f"Server has {len(existing):,} existing assets")
    else:
        existing = set()

    # Step 3: Get vision config — client config overrides tenant default
    from src.cli.config import load_config as _load_cli_config

    cli_cfg = _load_cli_config()
    ctx = client.get("/v1/tenant/context").json()

    vision_api_url = cli_cfg.vision_api_url or ctx.get("vision_api_url", "")
    vision_api_key = cli_cfg.vision_api_key or ctx.get("vision_api_key") or None
    vision_source = "client config" if cli_cfg.vision_api_url else "tenant config"

    if skip_vision:
        console.print("Vision AI: skipped (--skip-vision)")
    elif not vision_api_url:
        console.print("[red]Vision AI: not configured.[/red]")
        console.print("  Set it via: lumiverb config set --vision-api-url <url>")
        console.print("  Or to ingest without AI: lumiverb ingest --library <name> --skip-vision")
        raise SystemExit(1)
    else:
        console.print(f"Vision AI: {vision_model_id} via {vision_api_url} ({vision_source})")

    # Step 4: Filter to files that need ingestion
    to_ingest = []
    for f in local_files:
        if f["media_type"] == "video":
            with stats.lock:
                stats.skipped += 1
            continue
        if not force and f["rel_path"] in existing:
            with stats.lock:
                stats.skipped += 1
            continue
        to_ingest.append(f)

    console.print(f"[bold]Ingesting {len(to_ingest):,} images ({stats.skipped:,} skipped)...[/bold]")

    if not to_ingest:
        return stats

    # Step 5: Process and ingest concurrently
    pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="ingest")
    futures = []

    for f in to_ingest:
        fut = pool.submit(
            _process_and_ingest_one,
            client=client,
            library_id=library_id,
            root_path=root_path,
            rel_path=f["rel_path"],
            file_size=f["file_size"],
            file_mtime=f["file_mtime"],
            media_type=f["media_type"],
            vision_model_id=vision_model_id,
            vision_api_url=vision_api_url,
            vision_api_key=vision_api_key,
            skip_vision=skip_vision,
            stats=stats,
        )
        futures.append(fut)

    for fut in futures:
        fut.result()
    pool.shutdown(wait=True)

    return stats


# ---------------------------------------------------------------------------
# Vision backfill: add AI descriptions to assets that don't have them
# ---------------------------------------------------------------------------


def _backfill_one(
    *,
    client: LumiverbClient,
    asset_id: str,
    rel_path: str,
    vision_model_id: str,
    vision_api_url: str,
    vision_api_key: str | None,
    stats: _IngestStats,
) -> None:
    """Download proxy, call vision AI, POST results back."""
    try:
        resp = client.get(f"/v1/assets/{asset_id}/artifacts/proxy")
        proxy_bytes = resp.content

        vision_result = _call_vision_ai(
            proxy_bytes, vision_model_id, vision_api_url, vision_api_key,
        )
        if not vision_result:
            logger.warning("Vision returned no result for %s", rel_path)
            with stats.lock:
                stats.failed += 1
            print(f"vision \u2717 {rel_path}: no result", flush=True)
            return

        client.post(f"/v1/assets/{asset_id}/vision", json={
            "model_id": vision_result["model_id"],
            "model_version": vision_result["model_version"],
            "description": vision_result["description"],
            "tags": vision_result["tags"],
        })

        with stats.lock:
            stats.processed += 1
        print(f"vision \u2713 {rel_path}", flush=True)

    except Exception as e:
        logger.exception("Failed to backfill vision for %s: %s", rel_path, e)
        with stats.lock:
            stats.failed += 1
        print(f"vision \u2717 {rel_path}: {e}", flush=True)


def run_backfill_vision(
    client: LumiverbClient,
    library: dict,
    *,
    concurrency: int = 4,
    console: Console,
) -> _IngestStats:
    """Backfill AI descriptions for assets that don't have them."""
    from src.cli.config import load_config as _load_cli_config

    library_id = library["library_id"]
    vision_model_id = library.get("vision_model_id", "")

    # Resolve vision config (client > tenant)
    cli_cfg = _load_cli_config()
    ctx = client.get("/v1/tenant/context").json()
    vision_api_url = cli_cfg.vision_api_url or ctx.get("vision_api_url", "")
    vision_api_key = cli_cfg.vision_api_key or ctx.get("vision_api_key") or None
    vision_source = "client config" if cli_cfg.vision_api_url else "tenant config"

    if not vision_api_url:
        console.print("[red]Vision AI: not configured.[/red]")
        console.print("  Set it via: lumiverb config set --vision-api-url <url>")
        raise SystemExit(1)

    console.print(f"Vision AI: {vision_model_id} via {vision_api_url} ({vision_source})")

    # Page through assets missing vision
    console.print("Finding assets without AI descriptions...")
    to_backfill: list[dict] = []
    after: str | None = None
    while True:
        params: dict[str, str] = {
            "library_id": library_id,
            "limit": "500",
            "missing_vision": "true",
        }
        if after:
            params["after"] = after
        resp = client.get("/v1/assets/page", params=params)
        if resp.status_code == 204:
            break
        assets = resp.json()
        if not assets:
            break
        for a in assets:
            to_backfill.append(a)
        after = assets[-1]["asset_id"]

    stats = _IngestStats()
    console.print(f"[bold]Backfilling {len(to_backfill):,} assets...[/bold]")

    if not to_backfill:
        return stats

    pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="backfill")
    futures = []
    for a in to_backfill:
        fut = pool.submit(
            _backfill_one,
            client=client,
            asset_id=a["asset_id"],
            rel_path=a["rel_path"],
            vision_model_id=vision_model_id,
            vision_api_url=vision_api_url,
            vision_api_key=vision_api_key,
            stats=stats,
        )
        futures.append(fut)

    for fut in futures:
        fut.result()
    pool.shutdown(wait=True)

    return stats
