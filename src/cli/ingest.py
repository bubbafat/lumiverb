"""Per-asset ingest pipeline: discover files + process + create-on-ingest.

Walks the library root and processes each file:

Images: resize proxy, extract EXIF, call vision AI, then POST /v1/ingest.
Videos (stage 1): extract poster frame, extract EXIF, generate 10-sec preview,
  POST /v1/ingest + upload preview. Gets the UI looking right ASAP.
Videos (stage 2): scene detection + vision AI on each scene (run after stage 1).

The asset only appears on the server once it's fully populated — no partial state.
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, MofNCompleteColumn, TimeRemainingColumn, SpinnerColumn

from src.cli.client import LumiverbClient
from src.core.file_extensions import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from src.core.path_filter import PathFilter, is_path_included_merged
from src.workers.exif_extract import (
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


PROXY_LONG_EDGE = 2048
PROXY_JPEG_QUALITY = 75
PROXY_WEBP_QUALITY = 80
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def _jpeg_to_webp(jpeg_bytes: bytes) -> bytes:
    """Convert JPEG bytes to WebP using pyvips. Fast — no resize needed."""
    import pyvips

    img = pyvips.Image.new_from_buffer(jpeg_bytes, "")
    return img.write_to_buffer(".webp[Q=%d]" % PROXY_WEBP_QUALITY)


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
    from src.workers.exif_extract import parse_duration

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
    from src.cli.config import load_config as _load_cli_config
    from src.workers.captions.model_discovery import resolve_vision_model_id

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
    from src.video.clip_extractor import extract_video_frame
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


def _process_and_ingest_video_stage1(
    *,
    client: LumiverbClient,
    library_id: str,
    root_path: Path,
    rel_path: str,
    file_size: int,
    file_mtime: datetime | None,
    stats: "_IngestStats",
    progress: Progress | None = None,
    task_id: object = None,
) -> None:
    """Video stage 1: poster frame + EXIF + 10-sec preview → POST /v1/ingest + upload preview."""
    source_path = (root_path / rel_path).resolve()
    if not source_path.is_relative_to(root_path):
        logger.warning("Skipping %s: escapes library root", rel_path)
        with stats.lock:
            stats.failed += 1
        return

    try:
        # 1. Extract poster frame as JPEG proxy
        jpeg_bytes, width_orig, height_orig = _extract_video_poster(source_path)

        # 2. Extract EXIF
        exif_payload = _build_exif_payload(source_path, "video")

        # 3. Generate 10-second preview
        preview_bytes = _generate_video_preview(source_path)

        # 4. Convert poster to WebP for upload
        webp_bytes = _jpeg_to_webp(jpeg_bytes)
        del jpeg_bytes  # free JPEG buffer before HTTP calls

        # 5. POST /v1/ingest — create asset with poster frame as proxy
        files = {"proxy": ("proxy.webp", io.BytesIO(webp_bytes), "image/webp")}
        del webp_bytes  # BytesIO holds a copy; free the original
        data: dict[str, str] = {
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": str(file_size),
            "media_type": "video",
            "width": str(width_orig),
            "height": str(height_orig),
            "exif": json.dumps(exif_payload),
        }
        if file_mtime is not None:
            data["file_mtime"] = file_mtime.isoformat()

        resp = client.post("/v1/ingest", files=files, data=data)
        asset_id = resp.json()["asset_id"]

        # 6. Upload video preview
        client.post(
            f"/v1/assets/{asset_id}/artifacts/video_preview",
            files={"file": ("preview.mp4", io.BytesIO(preview_bytes), "video/mp4")},
        )

        with stats.lock:
            stats.processed += 1
        if progress is not None:
            task = progress.tasks[task_id]
            progress.advance(task_id, 1)
            progress.update(task_id, ok=task.fields["ok"] + 1)

    except Exception as e:
        logger.exception("Failed to ingest video %s: %s", rel_path, e)
        with stats.lock:
            stats.failed += 1
        if progress is not None:
            progress.console.print(f"[red]ingest \u2717[/red] {rel_path}: {e}")
            task = progress.tasks[task_id]
            progress.advance(task_id, 1)
            progress.update(task_id, fail=task.fields["fail"] + 1)


def _generate_clip_embedding(
    jpeg_bytes: bytes,
    clip_provider: object | None,
) -> dict | None:
    """Generate a CLIP embedding from JPEG proxy bytes. Returns embedding dict or None."""
    if clip_provider is None:
        return None
    try:
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        try:
            vector = clip_provider.embed_image(img)
        finally:
            img.close()
        return {
            "model_id": clip_provider.model_id,
            "model_version": clip_provider.model_version,
            "vector": vector,
        }
    except Exception as e:
        logger.warning("CLIP embedding failed: %s", e)
        return None


def _detect_faces(
    jpeg_bytes: bytes,
    face_provider: object | None,
) -> dict | None:
    """Detect faces in image. Returns face submission payload or None."""
    if face_provider is None:
        return None
    try:
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        detections = face_provider.detect_faces(img)
        img.close()
        del img
        payload = {
            "detection_model": face_provider.model_id,
            "detection_model_version": face_provider.model_version,
            "faces": [
                {
                    "bounding_box": d.bounding_box,
                    "detection_confidence": d.detection_confidence,
                    "embedding": d.embedding,
                }
                for d in detections
            ],
        }
        del detections
        return payload
    except Exception as e:
        logger.warning("Face detection failed: %s", e)
        return None


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
    from src.workers.faces.insightface_provider import InsightFaceProvider
    from PIL import Image as PILImage

    client = LumiverbClient(base_url=base_url, token=token)
    provider = InsightFaceProvider()
    provider.ensure_loaded()

    cache_path = Path(cache_dir) if cache_dir else None

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
            if image_bytes is None:
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

    client.close()
    return {"processed": processed, "failed": failed, "skipped": skipped, "errors": errors}


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
    vision_provider: object | None,
    clip_provider: object | None,
    stats: "_IngestStats",
    ingested_assets: list[dict] | None = None,
    proxy_cache: "ProxyCache | None" = None,
    progress: Progress | None = None,
    task_id: object = None,
) -> None:
    """Process one image file and POST /v1/ingest to create + populate atomically.

    Face detection is NOT run here — ONNX Runtime leaks ~35-70MB per inference
    in its C++ layer with no Python-level fix. Faces are detected in a
    subprocess-isolated post-pass after all images are ingested.
    """
    source_path = (root_path / rel_path).resolve()
    if not source_path.is_relative_to(root_path):
        logger.warning("Skipping %s: escapes library root", rel_path)
        with stats.lock:
            stats.failed += 1
        return

    try:
        # 1. Generate JPEG proxy (needed for vision AI compatibility)
        jpeg_bytes, width_orig, height_orig = _generate_proxy_bytes(source_path)

        # 2. Extract EXIF
        exif_payload = _build_exif_payload(source_path, media_type)

        # 3. Call vision AI with JPEG (optional)
        vision_payload = _call_vision_ai(
            jpeg_bytes, vision_model_id, vision_provider,
        )

        # 4. Generate CLIP embedding from JPEG proxy
        embedding = _generate_clip_embedding(jpeg_bytes, clip_provider)

        # 5. Convert to WebP for upload (server stores as-is, skips re-encoding)
        webp_bytes = _jpeg_to_webp(jpeg_bytes)
        # Cache JPEG proxy on disk for face detection post-pass
        _proxy_for_cache = jpeg_bytes
        del jpeg_bytes

        # 7. POST /v1/ingest — create asset + ingest atomically
        files = {"proxy": ("proxy.webp", io.BytesIO(webp_bytes), "image/webp")}
        del webp_bytes  # BytesIO holds a copy; free the original
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
        if embedding is not None:
            data["embeddings"] = json.dumps([embedding])

        resp = client.post("/v1/ingest", files=files, data=data)

        asset_id = resp.json().get("asset_id")
        if ingested_assets is not None and asset_id:
            if proxy_cache is not None:
                proxy_cache.put(asset_id, _proxy_for_cache)
            del _proxy_for_cache
            with stats.lock:
                ingested_assets.append({"asset_id": asset_id, "rel_path": rel_path})

        with stats.lock:
            stats.processed += 1
        if progress is not None:
            task = progress.tasks[task_id]
            progress.advance(task_id, 1)
            progress.update(task_id, ok=task.fields["ok"] + 1)

    except Exception as e:
        logger.exception("Failed to ingest %s: %s", rel_path, e)
        with stats.lock:
            stats.failed += 1
        if progress is not None:
            progress.console.print(f"[red]ingest \u2717[/red] {rel_path}: {e}")
            task = progress.tasks[task_id]
            progress.advance(task_id, 1)
            progress.update(task_id, fail=task.fields["fail"] + 1)


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


def _fetch_existing_assets(client: LumiverbClient, library_id: str) -> dict[str, str]:
    """Page through all assets on the server. Returns {rel_path: asset_id}."""
    existing: dict[str, str] = {}
    cursor: str | None = None
    while True:
        params: dict[str, str] = {"library_id": library_id, "limit": "500", "sort": "asset_id", "dir": "asc"}
        if cursor:
            params["after"] = cursor
        resp = client.get("/v1/assets/page", params=params)
        data = resp.json()
        items = data.get("items", [])
        if not items:
            break
        for a in items:
            existing[a["rel_path"]] = a["asset_id"]
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return existing


def run_ingest(
    client: LumiverbClient,
    library: dict,
    *,
    concurrency: int = 1,
    skip_vision: bool = False,
    skip_embeddings: bool = False,
    path_override: str | None = None,
    force: bool = False,
    media_type_filter: str = "all",
    dry_run: bool = False,
    console: Console,
) -> _IngestStats:
    """Discover files and ingest them.

    Processing order: all images first, then video stage 1 (poster + preview),
    then video stage 2 (scene detection + vision).

    1. Walk the filesystem to find media files.
    2. Fetch existing assets from the server (skip unless --force).
    3. Fetch tenant vision config.
    4. Images: proxy + EXIF + vision → POST /v1/ingest.
    5. Videos stage 1: poster frame + EXIF + 10-sec preview → POST /v1/ingest.
    6. Videos stage 2: scene detection + vision (future).
    """
    library_id = library["library_id"]
    root_path = Path(library["root_path"]).resolve()

    # Step 1: Load path filters (tenant + library)
    tenant_filters = _load_tenant_filters(client)
    library_filters = _load_library_filters(client, library_id)
    total_filters = len(tenant_filters) + len(library_filters)
    if total_filters:
        console.print(f"Loaded {len(tenant_filters)} tenant + {len(library_filters)} library filter(s)")

    # Step 2: Discover files (merged filters applied during walk)
    console.print("[bold]Discovering files...[/bold]")
    local_files = _walk_library(root_path, path_override, tenant_filters=tenant_filters, library_filters=library_filters)
    console.print(f"Found {len(local_files):,} media files")

    # Step 2b: Fetch existing assets (needed for skip detection and missing file cleanup)
    stats = _IngestStats()
    console.print("Checking server for existing assets...")
    existing = _fetch_existing_assets(client, library_id)
    console.print(f"Server has {len(existing):,} existing assets")

    # Step 2c: Detect and trash assets for files no longer on disk
    local_rel_paths = {f["rel_path"] for f in local_files}
    scope = existing
    if path_override:
        prefix = path_override.rstrip("/") + "/"
        scope = {rp: aid for rp, aid in existing.items() if rp.startswith(prefix)}
    missing_ids = [aid for rp, aid in scope.items() if rp not in local_rel_paths] if existing and root_path.is_dir() else []
    matched = len(scope) - len(missing_ids) if scope else 0
    new_files = [f for f in local_files if f["rel_path"] not in existing]

    if dry_run:
        console.print()
        console.print(f"[bold]Root path:[/bold]       {root_path} (exists: {'yes' if root_path.is_dir() else '[red]no[/red]'})")
        console.print(f"[bold]Files on disk:[/bold]   {len(local_files):,}")
        console.print(f"[bold]Assets on server:[/bold] {len(scope):,}")
        console.print(f"[bold]Matched:[/bold]         {matched:,}")
        console.print(f"[bold]Missing from disk:[/bold] {len(missing_ids):,}")
        console.print(f"[bold]New on disk:[/bold]      {len(new_files):,}")
        if missing_ids and len(missing_ids) <= 20:
            console.print("\n[bold]Missing files:[/bold]")
            missing_paths = [rp for rp, aid in scope.items() if aid in set(missing_ids)]
            for rp in sorted(missing_paths)[:20]:
                console.print(f"  {rp}")
        if new_files and len(new_files) <= 20:
            console.print("\n[bold]New files:[/bold]")
            for f in sorted(new_files, key=lambda f: f["rel_path"])[:20]:
                console.print(f"  {f['rel_path']}")
        return stats

    if missing_ids:
        console.print(f"Removing {len(missing_ids):,} assets no longer on disk...")
        for batch_start in range(0, len(missing_ids), 500):
            batch = missing_ids[batch_start : batch_start + 500]
            client.delete("/v1/assets", json={"asset_ids": batch})
        stats.removed = len(missing_ids)

    if not local_files:
        return stats

    # Step 3: Resolve vision config (client > tenant > auto-discover)
    vision_api_url, vision_api_key, vision_model_id, vision_source = _resolve_vision_config(client)

    include_images = media_type_filter in ("all", "image")
    include_videos = media_type_filter in ("all", "video")

    vision_provider = None
    if include_images:
        if skip_vision:
            console.print("Vision AI: skipped (--skip-vision)")
        elif not vision_api_url:
            console.print("[red]Vision AI: not configured.[/red]")
            console.print("  Set it via: lumiverb config set --vision-api-url <url>")
            console.print("  Or to ingest without AI: lumiverb ingest --library <name> --skip-vision")
            raise SystemExit(1)
        else:
            from src.workers.captions.factory import get_caption_provider
            vision_provider = get_caption_provider(vision_model_id, vision_api_url, vision_api_key)
            console.print(f"Vision AI: {vision_model_id} via {vision_api_url} ({vision_source})")

    # Step 3b: Load CLIP embedding model eagerly so we fail fast if it can't load
    clip_provider = None
    if include_images and not skip_embeddings:
        try:
            from src.workers.embeddings.clip_provider import CLIPEmbeddingProvider
            clip_provider = CLIPEmbeddingProvider()
            clip_provider._load()  # force weight loading now to fail fast
            console.print(f"CLIP embeddings: {clip_provider.model_version}")
        except Exception as e:
            console.print(f"[yellow]CLIP embeddings: unavailable ({e}) — continuing without embeddings[/yellow]")
            clip_provider = None
    elif skip_embeddings:
        console.print("CLIP embeddings: skipped (--skip-embeddings)")

    # Step 3c: Check InsightFace availability (loaded later in subprocess).
    # Do NOT load the model here — ONNX Runtime leaks ~35-70MB per inference
    # and the subprocess needs its own copy anyway.
    faces_available = False
    if include_images:
        try:
            from pathlib import Path as _Path
            _model_dir = _Path.home() / ".insightface" / "models" / "buffalo_l"
            if not _model_dir.is_dir():
                raise FileNotFoundError(f"Model not found at {_model_dir}")
            faces_available = True
            console.print("Face detection: buffalo_l (subprocess-isolated)")
        except Exception as e:
            console.print(f"[yellow]Face detection: unavailable ({e}) — continuing without faces[/yellow]")

    # Step 4: Separate images and videos
    images_to_ingest = []
    videos_to_ingest = []
    for f in local_files:
        if not force and f["rel_path"] in existing:
            with stats.lock:
                stats.skipped += 1
            continue
        if f["media_type"] == "video":
            if include_videos:
                videos_to_ingest.append(f)
            else:
                with stats.lock:
                    stats.skipped += 1
        else:
            if include_images:
                images_to_ingest.append(f)
            else:
                with stats.lock:
                    stats.skipped += 1

    if not images_to_ingest and not videos_to_ingest:
        console.print("Nothing to ingest.")
        return stats

    # One progress bar with a row per phase — all visible simultaneously.
    progress = _make_progress(console)
    with progress:
        img_tid = progress.add_task(
            "Images", total=len(images_to_ingest) or 1, ok=0, fail=0,
            visible=bool(images_to_ingest),
        )
        vid_tid = progress.add_task(
            "Videos", total=len(videos_to_ingest) or 1, ok=0, fail=0,
            visible=bool(videos_to_ingest),
        )

        # Step 5: Ingest images (bounded: at most `concurrency` in-flight)
        # Collect ingested asset IDs for face detection post-pass.
        ingested_assets: list[dict] = []
        proxy_cache = None
        if faces_available and images_to_ingest:
            from src.cli.proxy_cache import ProxyCache
            proxy_cache = ProxyCache()
        if images_to_ingest:
            pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="ingest")
            inflight: set[Future] = set()
            for f in images_to_ingest:
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
                    vision_provider=vision_provider,
                    clip_provider=clip_provider,
                    stats=stats,
                    ingested_assets=ingested_assets,
                    proxy_cache=proxy_cache,
                    progress=progress,
                    task_id=img_tid,
                )
                inflight.add(fut)
                if len(inflight) >= concurrency * 2:
                    done, inflight = _drain(inflight)
            _drain(inflight)
            pool.shutdown(wait=True)

        # Step 6: Video stage 1 — poster frame + EXIF + 10-sec preview
        if videos_to_ingest:
            pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="video")
            inflight = set()
            for f in videos_to_ingest:
                fut = pool.submit(
                    _process_and_ingest_video_stage1,
                    client=client,
                    library_id=library_id,
                    root_path=root_path,
                    rel_path=f["rel_path"],
                    file_size=f["file_size"],
                    file_mtime=f["file_mtime"],
                    stats=stats,
                    progress=progress,
                    task_id=vid_tid,
                )
                inflight.add(fut)
                if len(inflight) >= concurrency * 2:
                    done, inflight = _drain(inflight)
            _drain(inflight)
            pool.shutdown(wait=True)

    # TODO: Video stage 2 — scene detection + vision (ADR-005 Phase 6)

    # Step 7: Face detection in subprocess batches.
    # ONNX Runtime leaks ~35-70MB per inference call in its C++ layer — no
    # Python-level fix exists. Running each batch in a subprocess ensures all
    # native memory is reclaimed by the OS when the child exits. Model reload
    # (~2s per batch) is acceptable vs unbounded memory growth.
    if faces_available and ingested_assets:
        import multiprocessing as mp

        cache_dir = str(proxy_cache.path) if proxy_cache else None
        from src.cli.config import load_config
        FACE_BATCH_SIZE = load_config().face_batch_size
        console.print(f"\n[bold]Detecting faces ({len(ingested_assets):,} assets)...[/bold]")
        progress = _make_progress(console)
        try:
            with progress:
                face_tid = progress.add_task("Faces", total=len(ingested_assets), ok=0, fail=0)
                for batch_start in range(0, len(ingested_assets), FACE_BATCH_SIZE):
                    batch = ingested_assets[batch_start:batch_start + FACE_BATCH_SIZE]
                    try:
                        ctx = mp.get_context("spawn")
                        with ctx.Pool(1, initializer=_silence_subprocess_stdout) as pool:
                            result = pool.apply(
                                _face_batch_worker,
                                (client.base_url, client.token, batch, cache_dir),
                            )
                    except Exception as e:
                        logger.warning("Face batch failed: %s", e)
                        result = {"processed": 0, "failed": len(batch), "skipped": 0, "errors": []}

                    # Remove consumed entries from cache
                    if proxy_cache:
                        for item in batch:
                            proxy_cache.remove(item["asset_id"])

                    for err in result.get("errors", []):
                        progress.console.print(
                            f"[red]faces \u2717[/red] {err['rel_path']}: {err['error']}"
                        )

                    batch_total = result["processed"] + result["failed"] + result["skipped"]
                    task = progress.tasks[face_tid]
                    progress.advance(face_tid, batch_total)
                    progress.update(
                        face_tid,
                        ok=task.fields["ok"] + result["processed"],
                        fail=task.fields["fail"] + result["failed"],
                    )
        finally:
            if proxy_cache:
                proxy_cache.cleanup()

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
    vision_provider: object,
    stats: _IngestStats,
    progress: Progress | None = None,
    task_id: object = None,
) -> None:
    """Download proxy, call vision AI, POST results back."""
    try:
        resp = client.get(f"/v1/assets/{asset_id}/artifacts/proxy")
        proxy_bytes = resp.content

        vision_result = _call_vision_ai(
            proxy_bytes, vision_model_id, vision_provider,
        )
        if not vision_result:
            logger.warning("Vision returned no result for %s", rel_path)
            with stats.lock:
                stats.failed += 1
            if progress is not None:
                progress.console.print(f"[red]vision \u2717[/red] {rel_path}: no result")
                task = progress.tasks[task_id]
                progress.advance(task_id, 1)
                progress.update(task_id, fail=task.fields["fail"] + 1)
            return

        client.post(f"/v1/assets/{asset_id}/vision", json={
            "model_id": vision_result["model_id"],
            "model_version": vision_result["model_version"],
            "description": vision_result["description"],
            "tags": vision_result["tags"],
        })

        with stats.lock:
            stats.processed += 1
        if progress is not None:
            task = progress.tasks[task_id]
            progress.advance(task_id, 1)
            progress.update(task_id, ok=task.fields["ok"] + 1)

    except Exception as e:
        logger.exception("Failed to backfill vision for %s: %s", rel_path, e)
        with stats.lock:
            stats.failed += 1
        if progress is not None:
            progress.console.print(f"[red]vision \u2717[/red] {rel_path}: {e}")
            task = progress.tasks[task_id]
            progress.advance(task_id, 1)
            progress.update(task_id, fail=task.fields["fail"] + 1)


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

    from src.workers.captions.factory import get_caption_provider
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

    progress = _make_progress(console)
    with progress:
        tid = progress.add_task("Vision", total=len(to_backfill), ok=0, fail=0)
        pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="backfill")
        inflight: set[Future] = set()
        for a in to_backfill:
            fut = pool.submit(
                _backfill_one,
                client=client,
                asset_id=a["asset_id"],
                rel_path=a["rel_path"],
                vision_model_id=vision_model_id,
                vision_provider=vision_provider,
                stats=stats,
                progress=progress,
                task_id=tid,
            )
            inflight.add(fut)
            if len(inflight) >= concurrency * 2:
                done, inflight = _drain(inflight)
        _drain(inflight)
        pool.shutdown(wait=True)

    return stats
