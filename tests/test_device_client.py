# ruff: noqa: F811 — pytest fixtures are named as parameters
"""This machine as a device reading a library for a server (ADR-018 phase 6;
src/processing/device.py, `lumiverb device run`).

The real Device against the real API in-process, with a device's key and a
real folder (a photo and a video): one pass checks in, scans (the photo
and the video added with their previews), and makes what needs the
originals, probe facts, capture and the video's analysis proxy, through the
producers' own work; the server's list of what needs the originals is then
empty, and a second pass does nothing. A clip it can't make is reported as
failing, for its own libraries' clips and what it makes alone. Its
check-ins say what its scan has left to send, and go on during a long pass.
Its remakes wait while it's on battery, and say so.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

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
    # On mains power and an unmetered network, whatever this machine is on.
    return Lib(root, r.json()), Device(api, full_scan_every=0, name="Test Studio", held=lambda: None), api


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


class _ServerInNames(TestClient):
    """The real API as a server from ADR-024 part 3 answers a reader: its lists in names (reader-changes'
    made_with and paused, reader-work's work and remakes, GET /v1/producers' artifacts), at check-in
    protocol 2. What the reader sends goes to the real API as it is (it takes names from part 1)."""

    protocols: list[int]

    def request(self, method, url, **kwargs):
        import httpx

        from src.producers.names import file_named, name_of

        path = httpx.URL(str(url)).path
        if path.endswith("/readers/check-in"):
            self.protocols.append(kwargs["json"]["protocol"])
        r = super().request(method, url, **kwargs)
        if r.status_code != 200 or not (path.endswith(("/reader-changes", "/reader-work")) or path == "/v1/producers"):
            return r
        body = r.json()

        def named(words: list[str]) -> list[str]:
            return [file_named("proxy") if w == "previews" else name_of(w) for w in words]

        if "made_with" in body:
            body["made_with"] = {name_of(a): {**made, "producer": name_of(a)} for a, made in body["made_with"].items()}
            body["paused"] = {k: named(v) for k, v in body["paused"].items()}
        for clip in body.get("clips", []):
            clip["work"], clip["remakes"] = named(clip.get("work") or []), named(clip.get("remakes") or [])
        for row in body.get("producers", []):
            row["artifact"] = row["producer"] = name_of(row["artifact"])
        return httpx.Response(200, json=body, request=r.request)


def test_against_a_server_in_names_one_pass_makes_the_same(env, device):
    # ADR-024 phase 1 part 2: a part 2 reader speaks check-in protocol 2 and reads the lists in either spelling.
    from src.server.api.main import app

    lib, reader, api = device
    names = _ServerInNames(app, headers=dict(api._client.headers))
    names.protocols = []
    api._client = names
    _image(lib, "photos/a.png", 1)
    _video(lib, "videos/b.mp4")

    assert reader.once() == 1, "the analysis proxy, from the server's list in names"
    video = _clips(env, lib)["videos/b.mp4"]
    assert video["analysis_proxy_key"] and {"probe", "capture", "analysis_proxy"} <= set(video["made"])
    assert _reader_work(env, lib) == [], "nothing left that needs the originals"
    assert reader.once() == 0
    assert names.protocols and set(names.protocols) == {2}


def test_against_a_server_in_names_what_is_paused_waits(env, device):
    from src.server.api.main import app

    client, headers, *_ = env
    lib, reader, api = device
    names = _ServerInNames(app, headers=dict(api._client.headers))
    names.protocols = []
    api._client = names
    _video(lib, "videos/b.mp4")
    r = client.post("/v1/producers/lumiverb.video.analysis_proxy/pause", json={"scope": "work"}, headers=headers)
    assert r.status_code == 204, r.text
    try:
        reader.once()
        assert not _clips(env, lib)["videos/b.mp4"]["analysis_proxy_key"], "paused: told so in names, and waits"
    finally:
        client.post("/v1/producers/lumiverb.video.analysis_proxy/resume", json={"scope": "work"}, headers=headers)
    reader.once()
    assert _clips(env, lib)["videos/b.mp4"]["analysis_proxy_key"]


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


def _checked_in(env, lib: Lib) -> dict:
    client, headers, *_ = env
    return next(x for x in client.get("/v1/libraries", headers=headers).json()
                if x["library_id"] == lib.id)["checked_in"]


def test_its_check_ins_say_what_its_scan_has_left_to_send_and_go_on_during_a_long_pass(env, device):
    """A scan of new files hears how many it has left to send and their size
    on disk, then each one sent; a check-in during the scan (a long pass
    checks in every minute) says them, scanning; the one after says none."""
    lib, reader, _ = device
    files = [_image(lib, "a.png", 1), _image(lib, "b.png", 2), _video(lib, "c.mp4")]
    size = sum(f.stat().st_size for f in files)
    now = [1000.0]
    reader._clock = lambda: now[0]
    heard: list[tuple[int, int]] = []
    during: list[dict] = []
    told = reader.to_upload

    def to_upload(library_id: str, n: int, left: int) -> None:
        told(library_id, n, left)
        heard.append((n, left))
        if len(heard) == 1:  # the scan knows what it has to send, and a minute passes
            assert not reader.check_in_if_due(), "it checked in at the pass's start"
            now[0] += reader.check_in_every
            assert reader.check_in_if_due()
            during.append(_checked_in(env, lib))

    reader.to_upload = to_upload
    reader.once()

    assert heard[0] == (3, size) and heard[-1] == (0, 0), heard
    assert [n for n, _ in heard] == sorted((n for n, _ in heard), reverse=True)
    assert (during[0]["state"], during[0]["waiting_files"], during[0]["waiting_bytes"]) == ("scanning", 3, size)
    after = _checked_in(env, lib)
    assert (after["state"], after["waiting_files"], after["waiting_bytes"]) == ("idle", 0, 0)
    assert set(_clips(env, lib)) == {"a.png", "b.png", "c.mp4"}


def test_what_a_scan_stopped_part_way_left_is_said_until_its_next_scan(env, device):
    lib, reader, _ = device
    _image(lib, "a.png", 1)  # an empty folder is a drive not there: never scanned
    reader.once()
    reader.to_upload(lib.id, 212, 14 * 1024 ** 3)  # a scan stopped there
    reader.check_in()
    seen = _checked_in(env, lib)
    assert (seen["waiting_files"], seen["waiting_bytes"]) == (212, 14 * 1024 ** 3)
    reader.once()  # its next scan: nothing new on disk
    assert reader.to_send[lib.id] == (0, 0)
    assert _checked_in(env, lib)["waiting_files"] == 0


class _Server:
    """The check-in's answer, counted; the first one fails (the server away)."""

    def __init__(self) -> None:
        self.posts = 0
        self.checked = threading.Event()

    def post(self, path: str, **kw: Any):
        from unittest.mock import MagicMock

        assert path == "/v1/readers/check-in"
        self.posts += 1
        if self.posts == 1:
            raise ConnectionError("the server is away")
        if self.posts >= 3:
            self.checked.set()
        return MagicMock(json=lambda: {"libraries": [], "gone": [], "check_in_every_sec": 0.05})


def test_it_keeps_checking_in_during_a_pass_and_a_failed_one_is_tried_again():
    server = _Server()
    reader = Device(server, full_scan_every=0, held=lambda: None)
    reader.check_in_every = 0.05
    beat = threading.Thread(target=reader.keep_checking_in, kwargs={"every": 0.01}, daemon=True)
    beat.start()
    try:
        assert server.checked.wait(60), f"checked in {server.posts} time(s)"
    finally:
        reader.stopping.set()
        beat.join(60)
    assert not beat.is_alive()
    assert server.posts >= 3, "it went on after a check-in failed"


def _hashes(env, lib: Lib) -> dict[str, dict[str, str]]:
    """Each clip's previews' and analysis proxy's settings hash, by rel_path."""
    engine = create_engine(env[5])
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT a.rel_path, l.artifact, l.settings_hash FROM assets a"
                " JOIN artifact_lineage l ON l.asset_id = a.asset_id WHERE a.library_id = :l"
                " AND l.artifact IN ('proxy', 'video_preview', 'analysis_proxy')"), {"l": lib.id}).all()
    finally:
        engine.dispose()
    out: dict[str, dict[str, str]] = {}
    for rel_path, artifact, h in rows:
        out.setdefault(rel_path, {})[artifact] = h
    return out


def test_its_remakes_wait_on_battery_and_say_so_then_go_on(env, device):
    """ADR-018 decision 6: remakes wait for mains power; its check-ins and
    Processing's device wait say why; plugged in, it remakes."""
    from src.shared import producers as shared

    lib, reader, _ = device
    client, headers, *_ = env
    _image(lib, "a.png", 1)
    _video(lib, "b.mp4")
    reader.once()
    before = _hashes(env, lib)
    try:
        for artifact, notch in (("proxy", "best"), ("analysis_proxy", "good")):
            r = client.put(f"/v1/producers/{artifact}/settings", json={"settings": {"quality": notch}, "redo": True},
                           headers=headers)
            assert r.status_code == 200, r.text
        reader._held = lambda: "battery"
        reader.once()
        assert _hashes(env, lib) == before, "nothing remade on battery"
        seen = next(x for x in client.get("/v1/libraries", headers=headers).json() if x["library_id"] == lib.id)
        assert seen["checked_in"]["remakes_held"] == "battery"
        waits = {p["artifact"]: p["waits"] for p in client.get(
            "/v1/producers", params={"library_id": lib.id}, headers=headers).json()["producers"]}
        [wait] = [w for w in waits["analysis_proxy"] if w["reason"] == "device"]
        assert wait["remakes_held"] == "battery" and wait["clips"] == 1
        assert wait["device_name"].startswith("Studio") and wait["checked_in_at"]  # who, and when it was heard

        reader._held = lambda: None
        reader.once()
        after = _hashes(env, lib)
        best = shared.settings_hash(shared.effective_settings("proxy", {"quality": "best"}))
        good = shared.settings_hash(shared.effective_settings("analysis_proxy", {"quality": "good"}))
        assert after["a.png"]["proxy"] == best and after["b.mp4"]["proxy"] == best
        assert after["b.mp4"]["analysis_proxy"] == good
        seen = next(x for x in client.get("/v1/libraries", headers=headers).json() if x["library_id"] == lib.id)
        assert seen["checked_in"]["remakes_held"] is None
    finally:
        for artifact in ("proxy", "analysis_proxy"):
            client.put(f"/v1/producers/{artifact}/settings", json={"settings": {"quality": None}, "redo": True},
                       headers=headers)
