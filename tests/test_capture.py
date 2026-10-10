# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Capture facts, ADR-017 phase 3: taken_at is the camera's wall clock on
every scanner, its zone is taken_at_offset_min, and the capture producer
reads both (and the GPS accuracy) again from the originals of clips scanned
before, through the scheduler's runner."""

from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from src.processing.workers.exif_extract import (
    capture_facts,
    extract_exif_many,
    parse_taken_at,
    parse_taken_at_offset_min,
)
from tests.machine_lineage import made
from tests.producer_fakes import FakeAccount, due, run_job, scheduler_client
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture

UTC = timezone.utc


def _jpeg(path: Path, *, taken: str | None = None, offset: str | None = None, gps: tuple | None = None,
          accuracy: float | None = None, make: str = "Canon", model: str = "EOS R5") -> Path:
    """A real JPEG with the EXIF given, for exiftool to read."""
    from PIL import Image

    exif = Image.Exif()
    exif[0x010F], exif[0x0110] = make, model
    sub = {}
    if taken:
        sub[0x9003] = taken
    if offset:
        sub[0x9011] = offset
    if sub:
        exif[0x8769] = sub
    if gps:
        lat, lon = gps
        g = {1: "N" if lat >= 0 else "S", 2: (abs(lat), 0.0, 0.0), 3: "E" if lon >= 0 else "W",
             4: (abs(lon), 0.0, 0.0)}
        if accuracy is not None:
            g[31] = accuracy
        exif[0x8825] = g
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8)).save(path, exif=exif)
    return path


# ---------------------------------------------------------------------------
# Fast: what the file says
# ---------------------------------------------------------------------------


@pytest.mark.fast
@pytest.mark.parametrize("exif,when,offset", [
    ({"DateTimeOriginal": "2024:06:15 14:30:00"}, datetime(2024, 6, 15, 14, 30, tzinfo=UTC), None),
    ({"DateTimeOriginal": "2024:06:15 14:30:00", "OffsetTimeOriginal": "+02:00"},
     datetime(2024, 6, 15, 14, 30, tzinfo=UTC), 120),
    # A zone written in the date isn't applied: the wall clock stays, the zone is the offset.
    ({"DateTimeOriginal": "2024:06:15 14:30:00.25-05:00"}, datetime(2024, 6, 15, 14, 30, tzinfo=UTC), -300),
    ({"DateTimeOriginal": "2024-06-15T14:30:00Z"}, datetime(2024, 6, 15, 14, 30, tzinfo=UTC), 0),
    # A phone's video: its Keys date carries the zone; CreateDate (UTC by the spec) comes after.
    ({"CreationDate": "2024:06:15 14:30:00+05:45", "CreateDate": "2024:06:15 08:45:00"},
     datetime(2024, 6, 15, 14, 30, tzinfo=UTC), 345),
    ({"CreateDate": "2024:06:15 08:45:00"}, datetime(2024, 6, 15, 8, 45, tzinfo=UTC), None),
    ({"DateTimeOriginal": "0000:00:00 00:00:00", "CreateDate": "2024:06:15 08:45:00"},
     datetime(2024, 6, 15, 8, 45, tzinfo=UTC), None),
    # OffsetTimeOriginal wins over a zone in the date.
    ({"DateTimeOriginal": "2024:06:15 14:30:00+01:00", "OffsetTimeOriginal": "+02:00"},
     datetime(2024, 6, 15, 14, 30, tzinfo=UTC), 120),
    ({}, None, None),
])
def test_taken_at_is_the_wall_clock_and_the_zone_is_apart(exif, when, offset) -> None:
    assert parse_taken_at(exif) == when
    assert parse_taken_at_offset_min(exif) == offset


@pytest.mark.fast
@pytest.mark.parametrize("zone", ["America/New_York", "Asia/Kathmandu", "UTC", "Australia/Lord_Howe"])
def test_taken_at_doesnt_depend_on_this_machines_zone(zone, monkeypatch) -> None:
    """2:30 on the morning clocks went forward in New York doesn't exist there;
    the camera wrote it, so it's kept as written, wherever the scan runs."""
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    try:
        assert parse_taken_at({"DateTimeOriginal": "2024:03:10 02:30:00"}) == datetime(2024, 3, 10, 2, 30, tzinfo=UTC)
        assert parse_taken_at({"DateTimeOriginal": "2024:11:03 01:30:00"}) == datetime(2024, 11, 3, 1, 30, tzinfo=UTC)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


@pytest.mark.fast
def test_capture_facts_keep_accuracy_only_with_a_real_fix() -> None:
    exif = {"DateTimeOriginal": "2024:06:15 14:30:00", "OffsetTimeOriginal": "-04:00",
            "GPSLatitude": 40.7, "GPSLongitude": 74.0, "GPSLongitudeRef": "W", "GPSHPositioningError": 6.5}
    assert capture_facts(exif) == {"taken_at": "2024-06-15T14:30:00+00:00", "taken_at_offset_min": -240,
                                   "gps_accuracy_m": 6.5}
    assert capture_facts({**exif, "GPSLatitude": 0.0, "GPSLongitude": 0.0})["gps_accuracy_m"] is None
    assert capture_facts({}) == {"taken_at": None, "taken_at_offset_min": None, "gps_accuracy_m": None}


@pytest.mark.fast
def test_one_exiftool_reads_many_files_and_leaves_out_what_it_cant(tmp_path) -> None:
    a = _jpeg(tmp_path / "a.jpg", taken="2024:06:15 14:30:00", offset="+02:00")
    b = _jpeg(tmp_path / "b.jpg", taken="2024:06:16 09:00:00")
    tags = extract_exif_many([a, b, tmp_path / "gone.jpg"])
    assert set(tags) == {str(a), str(b)}
    assert tags[str(a)]["OffsetTimeOriginal"] == "+02:00"
    assert tags[str(b)]["DateTimeOriginal"] == "2024:06:16 09:00:00"


# ---------------------------------------------------------------------------
# Slow: the route, the ingest's lineage, and the producer through the runner
# ---------------------------------------------------------------------------


def _ingest(env, rel_path: str, exif: dict, *, says_capture: bool = True, library_id: str | None = None,
            sha: str | None = None) -> str:
    client, headers, lib, *_ = env
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (16, 16)).save(buf, format="JPEG")
    buf.seek(0)
    kinds = {"proxy": made("proxy")}
    if says_capture:
        kinds["capture"] = made("capture")
    r = client.post("/v1/ingest", headers=headers, files={"proxy": ("p.jpg", buf, "image/jpeg")}, data={
        "library_id": library_id or lib, "rel_path": rel_path, "file_size": "10", "media_type": "image",
        "lineage": json.dumps(kinds), "exif": json.dumps({"sha256": sha or os.urandom(32).hex(), **exif})})
    assert r.status_code == 200, r.text
    return r.json()["asset_id"]


def _row(env, asset_id: str) -> dict:
    engine = create_engine(env[-1])
    try:
        with engine.connect() as c:
            return dict(c.execute(text(
                "SELECT taken_at, taken_at_offset_min, gps_accuracy_m, gps_lat FROM assets WHERE asset_id = :a"),
                {"a": asset_id}).mappings().one())
    finally:
        engine.dispose()


def _library(env, root: Path) -> str:
    client, headers, *_ = env
    r = client.post("/v1/libraries", json={"name": f"cap-{os.urandom(3).hex()}", "root_path": str(root)},
                    headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["library_id"]


@pytest.mark.slow
def test_the_capture_route_stores_what_the_file_says(env) -> None:
    client, headers, *_ = env
    a = _ingest(env, "cap/route.jpg", {"taken_at": "2024-06-15T10:30:00+00:00", "gps_lat": 40.7, "gps_lon": -74.0})
    body = {"taken_at": "2024-06-15T14:30:00", "taken_at_offset_min": 120, "gps_accuracy_m": 7.5}
    r = client.put(f"/v1/assets/{a}/capture", json=body, headers=headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "lineage_required"
    r = client.put(f"/v1/assets/{a}/capture", json={**body, "lineage": made("capture")}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"changed": True}
    row = _row(env, a)
    assert row["taken_at"] == datetime(2024, 6, 15, 14, 30, tzinfo=UTC)
    assert (row["taken_at_offset_min"], row["gps_accuracy_m"]) == (120, 7.5)
    # The same again changes nothing; a file with no date now keeps the stored one.
    r = client.put(f"/v1/assets/{a}/capture", json={**body, "lineage": made("capture")}, headers=headers)
    assert r.json() == {"changed": False}
    r = client.put(f"/v1/assets/{a}/capture", json={"taken_at": None, "taken_at_offset_min": 120,
                                                   "lineage": made("capture")}, headers=headers)
    assert r.json() == {"changed": False}
    assert _row(env, a)["taken_at"] == datetime(2024, 6, 15, 14, 30, tzinfo=UTC)
    assert _row(env, a)["gps_accuracy_m"] is None  # the file said none this time
    # Accuracy without the file's GPS isn't kept; a zone out of range is refused.
    b = _ingest(env, "cap/nogps.jpg", {"taken_at": "2024-06-15T10:30:00+00:00"})
    r = client.put(f"/v1/assets/{b}/capture", json={"gps_accuracy_m": 3.0, "lineage": made("capture")},
                   headers=headers)
    assert r.status_code == 200 and _row(env, b)["gps_accuracy_m"] is None
    r = client.put(f"/v1/assets/{b}/capture", json={"taken_at_offset_min": 900, "lineage": made("capture")},
                   headers=headers)
    assert r.status_code == 422
    r = client.put("/v1/assets/ast_nope/capture", json={"lineage": made("capture")}, headers=headers)
    assert r.status_code == 404


@pytest.mark.slow
def test_a_scan_that_says_how_it_read_them_isnt_read_again_and_one_that_doesnt_is(env, tmp_path) -> None:
    lib = _library(env, tmp_path)
    sha = os.urandom(32).hex()
    python = _ingest(env, "py.jpg", {"taken_at": "2024-06-15T14:30:00+00:00"}, library_id=lib, sha=sha)
    mac = _ingest(env, "mac.jpg", {"taken_at": "2024-06-15T18:30:00+00:00"}, says_capture=False, library_id=lib)
    assert [c["asset_id"] for c in due(env[-1], "capture", [lib])] == [mac]
    # The Python scan sends it again without saying: read again too.
    assert _ingest(env, "py.jpg", {"taken_at": "2024-06-15T14:30:00+00:00"}, says_capture=False, library_id=lib,
                   sha=sha) == python
    assert {c["asset_id"] for c in due(env[-1], "capture", [lib])} == {python, mac}


@pytest.mark.slow
def test_the_producer_reads_the_originals_and_fixes_a_macs_taken_at(env, tmp_path) -> None:
    """The macOS app stored taken_at in its own zone (New York: 4 hours late in
    June). Read again on the brain, it's the camera's wall clock, with its zone."""
    from src.producers.capture.work import Capture

    lib = _library(env, tmp_path)
    _jpeg(tmp_path / "trip" / "a.jpg", taken="2024:06:15 14:30:00", offset="+02:00", gps=(48.85, 2.29), accuracy=4.0)
    _jpeg(tmp_path / "trip" / "b.jpg", taken="2024:06:15 15:00:00")
    a = _ingest(env, "trip/a.jpg", {"taken_at": "2024-06-15T18:30:00+00:00", "gps_lat": 48.85, "gps_lon": 2.29},
                says_capture=False, library_id=lib)
    b = _ingest(env, "trip/b.jpg", {"taken_at": "2024-06-15T19:00:00+00:00"}, says_capture=False, library_id=lib)
    gone = _ingest(env, "trip/gone.jpg", {}, says_capture=False, library_id=lib)
    items = due(env[-1], "capture", [lib])
    assert {c["asset_id"] for c in items} == {a, b, gone}

    acct = FakeAccount(scheduler_client(env[4]), {lib: tmp_path})
    outcome = run_job(Capture, acct, env[4], "capture", items)
    assert outcome == [gone]  # not on its storage: it waits for the scan that says so
    assert acct.failures.charged == []
    assert _row(env, a) == {"taken_at": datetime(2024, 6, 15, 14, 30, tzinfo=UTC), "taken_at_offset_min": 120,
                            "gps_accuracy_m": 4.0, "gps_lat": 48.85}
    assert _row(env, b)["taken_at"] == datetime(2024, 6, 15, 15, 0, tzinfo=UTC)
    assert _row(env, b)["taken_at_offset_min"] is None
    assert [c["asset_id"] for c in due(env[-1], "capture", [lib])] == [gone]


@pytest.mark.slow
def test_storage_away_tries_nothing(env, tmp_path) -> None:
    from src.producers.capture.work import Capture
    from src.producers.runner import NOT_TRIED

    lib = _library(env, tmp_path)
    _ingest(env, "x.jpg", {}, says_capture=False, library_id=lib)
    acct = FakeAccount(scheduler_client(env[4]), {lib: None})
    assert run_job(Capture, acct, env[4], "capture", due(env[-1], "capture", [lib])) == NOT_TRIED
    assert acct.away == [lib]
