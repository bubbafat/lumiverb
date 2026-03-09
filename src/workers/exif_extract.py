"""EXIF extraction via pyexiftool and SHA256 hashing."""

from __future__ import annotations

import hashlib
import logging
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
        if lat_ref == "S":
            lat = -lat
        if lon_ref == "W":
            lon = -lon
        return lat, lon
    except (TypeError, ValueError):
        return None, None


def parse_taken_at(exif: dict) -> datetime | None:
    """
    Parse DateTimeOriginal or CreateDate from EXIF dict.
    Returns UTC datetime or None.
    exiftool returns dates as "YYYY:MM:DD HH:MM:SS" strings.
    """
    raw = exif.get("DateTimeOriginal") or exif.get("CreateDate")
    if not raw:
        return None
    try:
        dt = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
