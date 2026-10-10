"""EXIF extraction via pyexiftool and SHA256 hashing."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from src.shared.location import is_fix

logger = logging.getLogger(__name__)

# Fields to extract. Focused list — full EXIF can be 300+ fields.
EXIF_FIELDS = [
    # Camera identity
    "Make",
    "Model",
    "LensModel",
    "LensID",
    "SerialNumber",
    "LensSerialNumber",
    # Capture settings
    "DateTimeOriginal",
    "CreationDate",  # QuickTime Keys (phones' videos): the wall clock with its zone
    "CreateDate",
    "OffsetTimeOriginal",
    "OffsetTime",
    "GPSDateTime",
    "ExposureTime",
    "FNumber",
    "ISO",
    "ExposureCompensation",
    "ExposureMode",
    "ExposureProgram",
    "MeteringMode",
    "Flash",
    "WhiteBalance",
    "FocalLength",
    "FocalLengthIn35mmFormat",
    "ShutterSpeedValue",
    "ApertureValue",
    # Image properties
    "ImageWidth",
    "ImageHeight",
    "Orientation",
    "ColorSpace",
    "BitsPerSample",
    # GPS
    "GPSLatitude",
    "GPSLongitude",
    "GPSAltitude",
    "GPSLatitudeRef",
    "GPSLongitudeRef",
    "GPSHPositioningError",
    # Copyright / creator
    "Artist",
    "Copyright",
    "Creator",
    # Software
    "Software",
    "ProcessingSoftware",
    # Video
    "Duration",
]


def extract_exif(source_path: Path) -> dict:
    """
    Extract EXIF metadata from file using pyexiftool.
    Returns dict of field -> value. Returns empty dict on failure.
    Strips namespace prefixes (e.g. "EXIF:Make" -> "Make").
    """
    try:
        import exiftool

        with exiftool.ExifToolHelper() as et:
            results = et.get_tags(str(source_path), tags=EXIF_FIELDS)
            if not results:
                return {}
            raw = results[0]
            cleaned = {}
            for k, v in raw.items():
                key = k.split(":")[-1] if ":" in k else k
                if key != "SourceFile":
                    cleaned[key] = v
            return cleaned
    except Exception as e:
        logger.warning("EXIF extraction failed for %s: %s", source_path, e)
        return {}


def _cleaned(raw: dict) -> dict:
    return {(k.split(":")[-1] if ":" in k else k): v for k, v in raw.items()
            if (k.split(":")[-1] if ":" in k else k) != "SourceFile"}


def extract_exif_many(paths: list[Path]) -> dict[str, dict]:
    """extract_exif for several files with one exiftool: {str(path): tags}.
    A file exiftool can't read is left out (the caller reads it alone).
    OSError (no exiftool, the mount's I/O) goes up."""
    if not paths:
        return {}
    import exiftool

    with exiftool.ExifToolHelper(check_execute=False) as et:
        results = et.get_tags([str(p) for p in paths], tags=EXIF_FIELDS)
    out: dict[str, dict] = {}
    for raw in results or []:
        source = raw.get("SourceFile")
        if source is not None:
            out[str(source)] = _cleaned(raw)
    return out


def compute_sha256(source_path: Path) -> str | None:
    """Compute SHA256 hash of file. Returns hex string or None on error."""
    try:
        h = hashlib.sha256()
        with open(source_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError as e:
        logger.warning("SHA256 failed for %s: %s", source_path, e)
        return None


def parse_gps(exif: dict) -> tuple[float | None, float | None]:
    """
    Parse GPS coordinates from EXIF dict.
    Returns (lat, lon) as signed floats, or (None, None).
    """
    try:
        lat = exif.get("GPSLatitude")
        lon = exif.get("GPSLongitude")
        lat_ref = exif.get("GPSLatitudeRef", "N")
        lon_ref = exif.get("GPSLongitudeRef", "E")
        if lat is None or lon is None:
            return None, None
        lat = float(lat)
        lon = float(lon)
        if lat_ref == "S" and lat > 0:
            lat = -lat
        if lon_ref == "W" and lon > 0:
            lon = -lon
        if not is_fix(lat, lon):
            return None, None
        return lat, lon
    except (TypeError, ValueError):
        return None, None


def parse_gps_accuracy_m(exif: dict) -> float | None:
    """GPSHPositioningError in metres, or None."""
    import math

    val = exif.get("GPSHPositioningError")
    if val is None:
        return None
    try:
        m = float(str(val).split()[0])
    except (ValueError, IndexError):
        return None
    return m if math.isfinite(m) and m >= 0 else None


_SUBSEC_RE = re.compile(r"\.\d+")
_OFFSET_RE = re.compile(r"^([+-])(\d{1,2}):?(\d{2})$")
# A zone at the end of a date string: "2024:05:01 14:02:03+02:00", "...Z".
_ZONE_SUFFIX_RE = re.compile(r"(Z|[+-]\d{1,2}:?\d{2})$")

# Where the time a clip was taken comes from, in order: the photo's EXIF,
# a phone video's QuickTime Keys (it carries its zone), then the container's
# CreateDate (QuickTime says UTC, but many cameras write their own clock).
_TAKEN_AT_KEYS = ("DateTimeOriginal", "CreationDate", "CreateDate")


def _offset_minutes(raw: object) -> int | None:
    """"+02:00", "-0530" or "Z" as minutes east of UTC; None otherwise."""
    s = str(raw).strip()
    if s in ("Z", "+00:00", "-00:00"):
        return 0
    m = _OFFSET_RE.match(s)
    if not m:
        return None
    minutes = int(m.group(2)) * 60 + int(m.group(3))
    if int(m.group(3)) >= 60 or minutes > 14 * 60:
        return None
    return -minutes if m.group(1) == "-" else minutes


def _taken_at_raw(exif: dict) -> str | None:
    for key in _TAKEN_AT_KEYS:
        raw = exif.get(key)
        if raw and str(raw).strip() and not str(raw).startswith("0000"):
            return str(raw).strip()
    return None


def parse_taken_at_offset_min(exif: dict) -> int | None:
    """The zone the clip was taken in, as minutes east of UTC:
    OffsetTimeOriginal (or OffsetTime), else a zone written in the date
    itself (a phone video's "2024:05:01 14:02:03+02:00"). None when the file
    doesn't say."""
    for key in ("OffsetTimeOriginal", "OffsetTime"):
        raw = exif.get(key)
        if raw is None:
            continue
        minutes = _offset_minutes(raw)
        if minutes is not None:
            return minutes
    raw = _taken_at_raw(exif)
    if raw:
        m = _ZONE_SUFFIX_RE.search(_SUBSEC_RE.sub("", raw))
        if m and re.search(r"\d{2}:\d{2}:\d{2}", raw):
            return _offset_minutes(m.group(1))
    return None


# Formats tried in order after stripping sub-seconds and timezone.
_TAKEN_AT_FORMATS = [
    "%Y:%m:%d %H:%M:%S",  # standard EXIF
    "%Y-%m-%dT%H:%M:%S",  # ISO 8601
    "%Y-%m-%d %H:%M:%S",  # space-separated ISO
]


def parse_taken_at(exif: dict) -> datetime | None:
    """When the clip was taken, as the camera's wall clock: the time the
    file writes, stored as if it were UTC (ADR-017). A zone written in the
    date isn't applied: it's the clip's taken_at_offset_min
    (parse_taken_at_offset_min), so every scanner stores the same taken_at
    for a file and the real instant is taken_at minus the offset, when known.
    None when the file doesn't say."""
    raw = _taken_at_raw(exif)
    if not raw:
        return None
    s = _SUBSEC_RE.sub("", raw)
    s = _ZONE_SUFFIX_RE.sub("", s).strip()
    for fmt in _TAKEN_AT_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    logger.debug("Could not parse taken_at from %r", raw)
    return None


def capture_facts(exif: dict) -> dict:
    """When and how precisely the file says it was taken (ADR-017): taken_at
    (ISO, the wall clock as UTC), taken_at_offset_min, and gps_accuracy_m
    when the file has a real GPS fix. The scan sends these; the capture
    producer reads them again for clips scanned before."""
    taken_at = parse_taken_at(exif)
    lat, _ = parse_gps(exif)
    return {
        "taken_at": taken_at.isoformat() if taken_at else None,
        "taken_at_offset_min": parse_taken_at_offset_min(exif),
        "gps_accuracy_m": parse_gps_accuracy_m(exif) if lat is not None else None,
    }


def parse_iso(exif: dict) -> int | None:
    """Extract ISO as integer."""
    val = exif.get("ISO")
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def parse_exposure_time_us(exif: dict) -> int | None:
    """Extract exposure time as integer microseconds.

    ExposureTime may be a decimal (0.004), a fraction string ("1/250"),
    or a numeric value.  Returns microseconds (e.g. 4000 for 1/250s).
    """
    val = exif.get("ExposureTime")
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        if "/" in s:
            num, den = s.split("/", 1)
            t = float(num) / float(den)
        else:
            t = float(s)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    import math
    if t <= 0 or not math.isfinite(t):
        return None
    return round(t * 1_000_000)


def parse_aperture(exif: dict) -> float | None:
    """Extract f-number from FNumber or ApertureValue."""
    for key in ("FNumber", "ApertureValue"):
        val = exif.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


def parse_focal_length(exif: dict, key: str = "FocalLength") -> float | None:
    """Extract focal length in mm. Handles '35 mm' and '35.0' formats."""
    val = exif.get(key)
    if val is None:
        return None
    try:
        return float(str(val).split()[0])
    except (ValueError, IndexError):
        return None


def parse_flash_fired(exif: dict) -> bool | None:
    """Parse the Flash EXIF string into a boolean."""
    flash = exif.get("Flash")
    if flash is None:
        return None
    flash_str = str(flash).lower()
    if "not fire" in flash_str or "no flash" in flash_str or "off" in flash_str:
        return False
    if "fired" in flash_str:
        return True
    return None


def parse_lens_model(exif: dict) -> str | None:
    """Extract lens model from LensModel or LensID."""
    return exif.get("LensModel") or exif.get("LensID") or None


def parse_orientation(exif: dict) -> int | None:
    """Extract EXIF orientation tag (1-8)."""
    val = exif.get("Orientation")
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def parse_duration(raw_exif: dict, is_video: bool) -> float | None:
    """Parse video duration from EXIF Duration field. Returns seconds or None."""
    if not is_video:
        return None
    val = raw_exif.get("Duration")
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        if ":" in s:
            parts = s.split(":")
            try:
                parts_f = [float(p) for p in parts]
            except ValueError:
                return None
            if len(parts_f) == 3:
                h, m, sec = parts_f
                return h * 3600 + m * 60 + sec
            if len(parts_f) == 2:
                m, sec = parts_f
                return m * 60 + sec
        try:
            return float(s)
        except ValueError:
            return None
    return None
