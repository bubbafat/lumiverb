"""Fast tests for EXIF extraction utilities."""

from pathlib import Path

import pytest


@pytest.mark.fast
def test_extract_exif_missing_file() -> None:
    from src.processing.workers.exif_extract import extract_exif

    assert extract_exif(Path("/nonexistent/file.jpg")) == {}


@pytest.mark.fast
def test_parse_gps_north_east() -> None:
    from src.processing.workers.exif_extract import parse_gps

    exif = {
        "GPSLatitude": 37.7749,
        "GPSLongitude": 122.4194,
        "GPSLatitudeRef": "N",
        "GPSLongitudeRef": "W",
    }
    lat, lon = parse_gps(exif)
    assert lat == pytest.approx(37.7749)
    assert lon == pytest.approx(-122.4194)


@pytest.mark.fast
def test_parse_gps_missing() -> None:
    from src.processing.workers.exif_extract import parse_gps

    assert parse_gps({}) == (None, None)


@pytest.mark.fast
def test_parse_taken_at_valid() -> None:
    from src.processing.workers.exif_extract import parse_taken_at

    exif = {"DateTimeOriginal": "2024:06:15 14:30:00"}
    dt = parse_taken_at(exif)
    assert dt is not None
    assert dt.year == 2024
    assert dt.month == 6
    assert dt.day == 15


@pytest.mark.fast
def test_parse_taken_at_missing() -> None:
    from src.processing.workers.exif_extract import parse_taken_at

    assert parse_taken_at({}) is None
