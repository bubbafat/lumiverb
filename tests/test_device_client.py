# ruff: noqa: F811 — pytest fixtures are named as parameters
"""This machine as a device reading a library for a server (ADR-018 phase 6;
src/processing/device.py, `lumiverb device run`).

The real Device against the real API in-process, with a device's key and a
real folder (a photo and a video): one pass checks in, scans (the photo
and the video added with their previews), and makes what needs the
originals, probe facts, capture and the video's analysis proxy, through the
producers' own work; the server's list of what needs the originals is then
empty, and a second pass does nothing. A clip it can't make is reported as
failing, for its own libraries' clips and what it makes alone.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from src.processing.device import Device
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_scan_outcomes import Lib, _image, _video

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def _cache_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _device_client(key: str):
    """A device's API client (its key), talking to the app in-process."""
    from src.client.cli.client import LumiverbClient
    from src.server.api.main import app

    client = LumiverbClient(base_url="http://testserver", token=key)
    client._client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return client


@pytest.fixture
def device(env, tmp_path: Path):
    """A registered device, a library it reads at a folder of its own, and its Device."""
    client, headers, *_ = env
    made = client.post("/v1/devices", json={"name": f"Studio {uuid.uuid4().hex[:6]}"}, headers=headers)
    assert made.status_code == 200, made.text
    root = tmp_path / "library"
    root.mkdir()
    r = client.post("/v1/libraries", headers=headers, json={
        "name": f"On the device {uuid.uuid4().hex[:8]}", "root_path": str(root),
        "reader": made.json()["device_id"]})
    assert r.status_code == 200, r.text
    api = _device_client(made.json()["key"])
    return Lib(root, r.json()), Device(api, full_scan_every=0, name="Test Studio"), api


def _clips(env, lib: Lib) -> dict[str, dict]:
    engine = create_engine(env[5])
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT a.asset_id, a.rel_path, a.proxy_key, a.analysis_proxy_key, a.duration_sec,"
                " array_remove(array_agg(l.artifact), NULL) AS made"
                " FROM active_assets a LEFT JOIN artifact_lineage l ON l.asset_id = a.asset_id"
                " WHERE a.library_id = :l"
                " GROUP BY a.asset_id, a.rel_path, a.proxy_key, a.analysis_proxy_key, a.duration_sec"), {"l": lib.id}).mappings().all()
    finally:
        engine.dispose()
    return {r["rel_path"]: dict(r) for r in rows}


def _reader_work(env, lib: Lib) -> list[dict]:
    client, headers, *_ = env
    r = client.get(f"/v1/libraries/{lib.id}/reader-work", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["clips"]


def test_one_pass_adds_the_files_and_makes_what_needs_them(env, device):
    lib, reader, _ = device
    _image(lib, "photos/a.png", 1)
    _video(lib, "videos/b.mp4")

    done = reader.once()

    clips = _clips(env, lib)
    assert set(clips) == {"photos/a.png", "videos/b.mp4"}
    assert all(c["proxy_key"] for c in clips.values()), "previews, from the scan"
    video = clips["videos/b.mp4"]
    assert video["analysis_proxy_key"] and video["duration_sec"] and video["duration_sec"] > 0.5
    assert {"probe", "capture", "analysis_proxy"} <= set(video["made"])
    assert "capture" in clips["photos/a.png"]["made"]
    assert done == 1, "the scan sends probe facts and capture; the work renders the analysis proxy"
    assert _reader_work(env, lib) == [], "nothing left that needs the originals"

    client, headers, *_ = env
    seen = next(x for x in client.get("/v1/libraries", headers=headers).json() if x["library_id"] == lib.id)
    assert seen["checked_in"]["client"] == "Test Studio"
    assert seen["checked_in"]["last_full_scan_at"] is not None

    assert reader.once() == 0, "a second pass: nothing to do"
    assert set(_clips(env, lib)) == {"photos/a.png", "videos/b.mp4"}


def test_it_reads_only_its_own_libraries(env, device, tmp_path: Path):
    lib, reader, api = device
    client, headers, *_ = env
    other = tmp_path / "other"
    other.mkdir()
    theirs = client.post("/v1/libraries", headers=headers, json={
        "name": f"The server's {uuid.uuid4().hex[:8]}", "root_path": str(other)}).json()
    reader.check_in()
    assert list(reader.libraries) == [lib.id]
    # It reports failures only for its own clips, and only for what it makes.
    _image(lib, "a.png", 7)
    reader.once()
    mine = _clips(env, lib)["a.png"]["asset_id"]
    ok = api.raw("POST", "/v1/producers/failures", json={"items": [
        {"asset_id": mine, "artifact": "capture", "error": "exiftool said no"}]})
    assert ok.status_code == 200 and ok.json() == {"recorded": 1}, ok.text
    refused = api.raw("POST", "/v1/producers/failures", json={"items": [
        {"asset_id": mine, "artifact": "vision", "error": "x"}]})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "not_for_devices"
    from tests.clips import make_clip

    other_clip = make_clip(env[5], theirs["library_id"], "b.jpg")
    elsewhere = api.raw("POST", "/v1/producers/failures", json={"items": [
        {"asset_id": mine, "artifact": "probe", "error": "x"}, {"asset_id": other_clip, "artifact": "probe", "error": "x"}]})
    assert elsewhere.status_code == 404
    assert api.raw("GET", f"/v1/libraries/{lib.id}/changes").status_code == 200
    assert api.raw("GET", f"/v1/libraries/{theirs['library_id']}/changes").status_code == 404


def test_a_library_handed_away_is_dropped_at_the_next_check_in(env, device):
    lib, reader, _ = device
    client, headers, *_ = env
    reader.check_in()
    assert lib.id in reader.libraries and lib.id in reader.marks
    r = client.put(f"/v1/libraries/{lib.id}/reader", json={"reader": "server"}, headers=headers)
    assert r.status_code == 200, r.text
    assert reader.check_in() == [lib.id]
    assert reader.libraries == {} and reader.marks == {}
    assert reader.once() == 0


def test_its_work_makes_probe_facts_and_capture_through_the_producers(env, device):
    """A clip without them (made before its reader was this device, say): the
    server lists them, and the work makes and saves them with their lineage."""
    from tests.clips import make_clip

    lib, reader, _ = device
    _video(lib, "old/c.mp4")
    clip = make_clip(env[5], lib.id, "old/c.mp4", media_type="video")
    reader.check_in()
    reader.look()
    listed = {c["asset_id"]: c["work"] for c in _reader_work(env, lib)}
    assert {"probe", "capture"} <= set(listed[clip])
    reader.work()
    made = next(c for c in _clips(env, lib).values() if c["asset_id"] == clip)
    assert {"probe", "capture"} <= set(made["made"]) and made["duration_sec"] > 0.5
    assert clip not in {c["asset_id"] for c in _reader_work(env, lib) if {"probe", "capture"} & set(c["work"])}
