"""EXIF extraction via pyexiftool and SHA256 hashing."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

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
    "CreateDate",
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
        return lat, lon
    except (TypeError, ValueError):
        return None, None


_SUBSEC_RE = re.compile(r"\.\d+")

# Formats tried in order after stripping sub-seconds and timezone.
_TAKEN_AT_FORMATS = [
    "%Y:%m:%d %H:%M:%S",  # standard EXIF
    "%Y-%m-%dT%H:%M:%S",  # ISO 8601
    "%Y-%m-%d %H:%M:%S",  # space-separated ISO
]


def parse_taken_at(exif: dict) -> datetime | None:
    """
    Parse DateTimeOriginal or CreateDate from EXIF dict.
    Returns UTC datetime or None.
    Handles sub-second precision and timezone offsets from various camera manufacturers.
    """
    raw = exif.get("DateTimeOriginal") or exif.get("CreateDate")
    if not raw:
        return None
    s = _SUBSEC_RE.sub("", str(raw).strip())
    # Try with a trailing timezone offset (%z handles ±HH:MM in Python 3.7+)
    for fmt in _TAKEN_AT_FORMATS:
        for candidate in (s, s.replace(" ", "T")):
            for suffix in ("", "%z"):
                try:
                    dt = datetime.strptime(candidate, fmt + suffix)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt
                except ValueError:
                    continue
    logger.debug("Could not parse taken_at from %r", raw)
    return None


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
    if t <= 0:
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
