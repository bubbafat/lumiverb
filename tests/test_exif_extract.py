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


@pytest.mark.fast
@pytest.mark.parametrize("name", ["two\nlines.jpg", "a\rreturn.jpg", "tab\there.jpg"])
def test_a_path_with_a_control_character_is_never_handed_to_exiftool(tmp_path, monkeypatch, name) -> None:
    # pyexiftool sends exiftool its paths a line each: a line break in one would be more of exiftool's arguments.
    import exiftool

    from src.processing.workers.exif_extract import extract_exif, extract_exif_many

    good, odd = tmp_path / "good.jpg", tmp_path / "folder" / name
    odd.parent.mkdir()
    for path in (good, odd):
        path.write_bytes(b"not a picture")
    sent: list[str] = []

    def get_tags(self, files, tags=None, params=None):
        sent.extend([files] if isinstance(files, str) else files)
        return [{"SourceFile": f} for f in ([files] if isinstance(files, str) else files)]

    monkeypatch.setattr(exiftool.ExifToolHelper, "get_tags", get_tags)
    monkeypatch.setattr(exiftool.ExifToolHelper, "run", lambda self: None)
    monkeypatch.setattr(exiftool.ExifToolHelper, "terminate", lambda self, **kw: None)

    assert set(extract_exif_many([good, odd])) == {str(good)}
    assert extract_exif(odd) == {}
    assert sent == [str(good)]
