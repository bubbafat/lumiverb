"""What a scan makes of a file (src/processing/scan.py): its proxy and
thumbnail, EXIF, a video's poster and preview; and a library's filters.
Which files a library has is its originals' walk (src/processing/originals.py).
The rest is the scheduler's (src/producers/).
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.shared.content_hash import content_hash as hash_of
from src.shared.content_hash import sampled, whole_file_sha256
from src.processing.workers.exif_extract import (
    extract_exif,
    parse_aperture,
    parse_flash_fired,
    parse_focal_length,
    parse_gps,
    parse_iso,
    parse_lens_model,
    parse_orientation,
    parse_exposure_time_us,
)

logger = logging.getLogger(__name__)


from src.processing.proxy.proxy_gen import KEEP, PROXY_JPEG_QUALITY, PROXY_LONG_EDGE, jpeg_format

# The previews' quality at the registry's default notch, better (src/producers/proxy WEBP, video_preview):
# a scan makes them at the account's (scan.py Previews), these when the server doesn't say.
PROXY_WEBP_QUALITY = 80
PREVIEW_CRF = 28


def _jpeg_to_webp(jpeg_bytes: bytes, quality: int = PROXY_WEBP_QUALITY) -> bytes:
    """Convert JPEG bytes to WebP using pyvips. Fast — no resize needed."""
    from src.processing.libvips import pyvips

    img = pyvips.Image.new_from_buffer(jpeg_bytes, "")
    return img.write_to_buffer(f".webp[Q={quality},{KEEP}]")


def _generate_proxy_bytes(source_path: Path, jpeg_quality: int = PROXY_JPEG_QUALITY) -> tuple[bytes, int, int]:
    """Generate a resized JPEG proxy from a source image. Returns (bytes, width_orig, height_orig)."""
    from src.processing.proxy.proxy_gen import generate_proxy_bytes
    return generate_proxy_bytes(source_path, jpeg_quality=jpeg_quality)


def _content_hash(source_path: Path) -> str | None:
    try:
        return hash_of(source_path)
    except OSError as exc:
        logger.warning("Couldn't hash %s: %s", source_path, exc)
        return None


def _build_exif_payload(source_path: Path, media_type: str, *, content_hash: str | None = None,
                        previous_hash: str | None = None, whole_too: bool = False) -> dict:
    """Extract EXIF and build the JSON payload for the ingest endpoint, with
    the file's content hash (content_hash: already known, else read now).
    The whole file's "sha256" goes beside an fp1 (previous_hash) when it's
    known or whole_too asks for it (its scan's plan: legacy,
    src/shared/content_hash.py)."""
    from src.processing.workers.exif_extract import capture_facts, parse_duration

    exif_data = extract_exif(source_path)
    content_hash = content_hash or _content_hash(source_path)
    if sampled(content_hash) and whole_too and previous_hash is None:
        previous_hash = whole_file_sha256(source_path)
    gps_lat, gps_lon = parse_gps(exif_data)
    duration_sec = parse_duration(exif_data, media_type == "video")

    return {
        "content_hash": content_hash,
        **({"previous_hash": previous_hash} if sampled(content_hash) and previous_hash else {}),
        "exif": exif_data,
        "camera_make": exif_data.get("Make"),
        "camera_model": exif_data.get("Model"),
        "gps_lat": gps_lat,
        "gps_lon": gps_lon,
        **capture_facts(exif_data),  # taken_at, taken_at_offset_min, gps_accuracy_m
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


def _probe_video_dimensions(source_path: Path) -> tuple[int, int]:
    """Get the original video dimensions via ffprobe. Returns (width, height)."""
    import subprocess

    from src.processing.video import ffmpeg_run

    cmd = [
        "ffprobe", "-v", "error",
        *ffmpeg_run.input_args(),
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


def _extract_video_poster(source_path: Path, jpeg_quality: int = PROXY_JPEG_QUALITY) -> tuple[bytes, int, int]:
    """Extract a poster frame (first frame) from a video. Returns (jpeg_bytes, width, height)."""
    import tempfile

    from src.processing.libvips import pyvips, thumbnail_file
    from src.processing.video.clip_extractor import extract_video_frame

    width_orig, height_orig = _probe_video_dimensions(source_path)

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        ok = extract_video_frame(source_path, tmp_path, timestamp=0.0)
        if not ok or not tmp_path.exists() or tmp_path.stat().st_size == 0:
            raise RuntimeError(f"Failed to extract poster frame from {source_path}")

        proxy_img = thumbnail_file(
            tmp_path, PROXY_LONG_EDGE,
            height=PROXY_LONG_EDGE,
            size=pyvips.enums.Size.DOWN,
        ).copy_memory()
        proxy_bytes = proxy_img.write_to_buffer(jpeg_format(jpeg_quality))
        return proxy_bytes, width_orig, height_orig
    finally:
        tmp_path.unlink(missing_ok=True)


def _generate_video_preview(source_path: Path, crf: int = PREVIEW_CRF) -> bytes:
    """Generate a 10-second MP4 preview clip, at x264's crf (its quality
    notch's, ADR-018 decision 6). Returns preview bytes.

    Decoded by a Mac's media engine where there is one (ffmpeg_run.SHARED_MEMORY:
    a fifth of the CPU's work on a 4K or 5.3K original, and a scan makes
    several at once), else on the CPU; one the hardware fails at, at the
    start or on any picture (ffmpeg_run.hwaccel_args), is made again on the
    CPU, and one with a broken audio track (some camera MOVs)
    again without audio. ffmpeg is stopped only when it makes no progress."""
    import tempfile

    from src.processing.video import ffmpeg_run

    crf = ffmpeg_run.whole(crf, 0, 51, "crf")  # a number from settings, before it's in a command
    PREVIEW_DURATION_SEC = 10
    PREVIEW_MAX_HEIGHT = 720

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        preview_path = Path(tmp.name)

    def command(hwaccel: str | None, audio: bool) -> list[str]:
        return [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            *ffmpeg_run.hwaccel_args(hwaccel),
            *ffmpeg_run.input_args(),
            "-ss", "0", "-i", str(source_path),
            "-t", str(PREVIEW_DURATION_SEC),
            # Limited range, as players expect: a full-range source (DJI's 1080p) is converted.
            "-vf", f"scale=-2:'min({PREVIEW_MAX_HEIGHT},ih)':out_range=tv,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            *(["-c:a", "aac", "-ac", "2", "-b:a", "128k"] if audio else ["-an"]),
            # No metadata: phones and drones write where they were (GPS) into the file.
            "-map_metadata", "-1", "-map_chapters", "-1",
            "-movflags", "+faststart",
            str(preview_path),
        ]

    engine = ffmpeg_run.decoded_by("auto")
    attempts = [(engine, True)] if engine in ffmpeg_run.SHARED_MEMORY else []
    attempts += [(None, True), (None, False)]
    try:
        said = ""
        for hwaccel, audio in attempts:
            preview_path.unlink(missing_ok=True)
            result = ffmpeg_run.run(command(hwaccel, audio))
            if result.returncode == 0 and preview_path.is_file() and preview_path.stat().st_size > 0:
                return preview_path.read_bytes()
            said = result.stderr.strip()[-500:] or f"exit {result.returncode}"
        raise RuntimeError(f"ffmpeg made no preview of {source_path}: {said}")
    finally:
        preview_path.unlink(missing_ok=True)
