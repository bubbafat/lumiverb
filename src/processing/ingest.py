"""What a scan makes of a file (src/processing/scan.py): its proxy and
thumbnail, EXIF, a video's poster and preview; and which files a library
has (its filters). The rest is the scheduler's (src/producers/).
"""

from __future__ import annotations

import logging
import os
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from stat import S_ISREG


from src.processing.api import ApiClient
from src.shared.file_extensions import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from src.shared.path_filter import PathFilter, is_path_included_merged
from src.shared.io_utils import UnsafeRelPathError, resolve_source_path, stat_if_present
from src.processing.workers.exif_extract import (
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


from src.processing.proxy.proxy_gen import PROXY_LONG_EDGE, PROXY_JPEG_QUALITY

PROXY_WEBP_QUALITY = 80
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def _jpeg_to_webp(jpeg_bytes: bytes) -> bytes:
    """Convert JPEG bytes to WebP using pyvips. Fast — no resize needed."""
    import pyvips

    img = pyvips.Image.new_from_buffer(jpeg_bytes, "")
    return img.write_to_buffer(".webp[Q=%d]" % PROXY_WEBP_QUALITY)


def _generate_proxy_bytes(source_path: Path) -> tuple[bytes, int, int]:
    """Generate a resized JPEG proxy from a source image. Returns (bytes, width_orig, height_orig)."""
    from src.processing.proxy.proxy_gen import generate_proxy_bytes
    return generate_proxy_bytes(source_path)


def _build_exif_payload(source_path: Path, media_type: str) -> dict:
    """Extract EXIF and build the JSON payload for the ingest endpoint."""
    from src.processing.workers.exif_extract import parse_duration, parse_gps_accuracy_m, parse_taken_at_offset_min

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
        "gps_accuracy_m": parse_gps_accuracy_m(exif_data) if gps_lat is not None else None,
        "taken_at_offset_min": parse_taken_at_offset_min(exif_data),
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
    from src.processing.video.clip_extractor import extract_video_frame
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
        # No metadata: phones and drones write where they were (GPS) into the file.
        "-map_metadata", "-1", "-map_chapters", "-1",
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
            "-an", "-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart",
            str(preview_path),
        ]
        subprocess.run(no_audio_cmd, check=True, capture_output=True)

    try:
        if not preview_path.exists() or preview_path.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg produced no output for {source_path}")
        return preview_path.read_bytes()
    finally:
        preview_path.unlink(missing_ok=True)


def _walk_library(
    root_path: Path,
    path_prefix: str | None = None,
    tenant_filters: list[PathFilter] | None = None,
    library_filters: list[PathFilter] | None = None,
    unlisted: list[str] | None = None,
) -> list[dict]:
    """Walk the library root and return a list of file descriptors.

    Each entry: {rel_path, file_size, file_mtime, media_type, ext}.
    Files that don't pass the merged tenant + library filters are silently skipped.
    Hidden files are listed; symlinks aren't followed. Folders that
    can't be listed, or that hold a media file that can't be checked, go in
    `unlisted` as rel paths ("" is the library root), so the caller doesn't
    take their files for deleted.
    """
    walk_root = root_path
    if path_prefix:
        try:
            walk_root = resolve_source_path(root_path, path_prefix)
        except UnsafeRelPathError:
            logger.warning("Not scanning %s: it leaves the library", path_prefix)
            if unlisted is not None:
                unlisted.append(path_prefix)  # its files aren't taken for deleted
            return []

    def _unreadable(exc: OSError, folder: str | None = None) -> None:
        folder = folder or exc.filename or str(walk_root)
        logger.warning("Can't list %s: %s", folder, exc)
        if unlisted is not None:
            rel = os.path.relpath(folder, root_path)
            rel = "" if rel == "." else unicodedata.normalize("NFC", rel)
            if rel not in unlisted:
                unlisted.append(rel)

    has_filters = bool(tenant_filters or library_filters)
    t_filters = tenant_filters or []
    l_filters = library_filters or []

    found: list[Path] = []
    for folder, _dirs, names in os.walk(walk_root, onerror=_unreadable):
        found.extend(Path(folder, name) for name in names)

    results = []
    for p in sorted(found):
        # Only files the scan would take are stat'ed: one that can't be
        # checked holds back deletions in its folder.
        ext = p.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            continue

        # NFC, as the macOS scanner stores it: one identity per file whichever
        # machine scans it. Disk access goes through resolve_source_path.
        rel_path = unicodedata.normalize("NFC", str(p.relative_to(root_path)))

        if has_filters and not is_path_included_merged(rel_path, t_filters, l_filters):
            continue

        try:
            stat = stat_if_present(p, follow_symlinks=False)  # a symlink isn't S_ISREG
        except OSError as exc:
            _unreadable(exc, str(p.parent))
            continue
        if stat is None or not S_ISREG(stat.st_mode):
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


def _load_tenant_filters(client: ApiClient) -> list[PathFilter]:
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


def _load_library_filters(client: ApiClient, library_id: str) -> list[PathFilter]:
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
