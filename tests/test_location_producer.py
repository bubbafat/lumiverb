# ruff: noqa: F811 — pytest fixtures are named as parameters
"""The location producer against the real API and database (ADR-017 phase 3):
camera clips placed from phone photos across folders, a person's location
never written over and counted as a fix, guesses never used as fixes, the
guesses around a fix checked again when it changes, goes or comes back, or
the window changes (kept unless the new one is surer, replaced when the old
one is wrong), the clock offset from scene pairs, and turning it off."""

from __future__ import annotations

import io
import json
import math
import os
import random

import pytest
from sqlalchemy import create_engine, text

from src.producers.location.infer import Stamp, gap_minutes, parse_time
from src.producers.location.work import Locate
from tests.machine_lineage import made
from tests.producer_fakes import FakeAccount, due, run_job, scheduler_client
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture

pytestmark = pytest.mark.slow

PHONE = {"camera_make": "Apple", "camera_model": "iPhone 15 Pro"}
CAMERA = {"camera_make": "Blackmagic Design", "camera_model": "Pocket Cinema Camera 6K"}


def _library(env, name: str = "") -> str:
    client, headers, *_ = env
    r = client.post("/v1/libraries", json={"name": f"loc-{name}-{os.urandom(3).hex()}", "root_path": "/tmp/loc"},
                    headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["library_id"]


def _clip(env, lib: str, rel_path: str, when: str, *, offset: int | None = None,
          gps: tuple[float, float] | None = None, device: dict = CAMERA, serial: str | None = None,
          sha: str | None = None) -> str:
    """A clip as the Python scan ingests it: taken_at the wall clock, its zone apart."""
    client, headers, *_ = env
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (16, 16)).save(buf, format="JPEG")
    buf.seek(0)
    exif = {"sha256": sha or os.urandom(32).hex(), **device, "taken_at": f"{when}+00:00",
            "taken_at_offset_min": offset, "exif": {"SerialNumber": serial} if serial else {}}
    if gps:
        exif |= {"gps_lat": gps[0], "gps_lon": gps[1]}
    r = client.post("/v1/ingest", headers=headers, files={"proxy": ("p.jpg", buf, "image/jpeg")}, data={
        "library_id": lib, "rel_path": rel_path, "file_size": "10", "media_type": "image",
        "lineage": json.dumps({"proxy": made("proxy"), "capture": made("capture")}), "exif": json.dumps(exif)})
    assert r.status_code == 200, r.text
    return r.json()["asset_id"]


def _settings(env, *, on: bool = True, minutes: int = 360) -> None:
    client, headers, *_ = env
    r = client.put("/v1/producers/location/settings", headers=headers,
                   json={"settings": {"infer_location": on, "inference_minutes": minutes}, "redo": True})
    assert r.status_code == 200, r.text


def _run(env, lib: str, only: list[str] | None = None):
    items = [c for c in due(env[-1], "location", [lib]) if only is None or c["asset_id"] in only]
    acct = FakeAccount(scheduler_client(env[4]))
    outcome = run_job(Locate, acct, env[4], "location", items)
    assert acct.failures.charged == [], acct.failures.charged
    return outcome, [c["asset_id"] for c in items]


def _sql(env, query: str, **params):
    engine = create_engine(env[-1])
    try:
        with engine.connect() as c:
            return [dict(r) for r in c.execute(text(query), params).mappings().all()]
    finally:
        engine.dispose()


def _location(env, asset_id: str) -> dict | None:
    rows = _sql(env, "SELECT lat, lon, radius_m, source, status, basis FROM asset_location WHERE asset_id = :a",
                a=asset_id)
    return rows[0] if rows else None


def _lineage(env, asset_id: str) -> dict | None:
    rows = _sql(env, "SELECT producer, outcome FROM artifact_lineage WHERE asset_id = :a AND artifact = 'location'",
                a=asset_id)
    return rows[0] if rows else None


def _due(env, lib: str) -> set[str]:
    return {c["asset_id"] for c in due(env[-1], "location", [lib])}


def _person(env, asset_ids: list[str], lat: float, lon: float, replace: str = "none"):
    client, headers, *_ = env
    r = client.put("/v1/assets/locations", json={"asset_ids": asset_ids, "lat": lat, "lon": lon,
                                                 "replace": replace}, headers=headers)
    assert r.status_code == 200, r.text


def _context(env, asset_id: str, minutes: int = 360, clock: float = 0.0) -> dict:
    client, headers, *_ = env
    r = client.get(f"/v1/assets/locations/context/{asset_id}", params={"minutes": minutes, "clock_offset_min": clock},
                   headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_nothing_is_guessed_while_infer_location_is_off(env) -> None:
    _settings(env, on=False)
    lib = _library(env, "off")
    _clip(env, lib, "Phone/p.jpg", "2024-06-15T14:00:00", offset=120, gps=(10.0, -61.5), device=PHONE)
    _clip(env, lib, "BMC/c.jpg", "2024-06-15T14:30:00")
    assert _due(env, lib) == set()


def test_camera_clips_are_placed_from_phone_photos_across_folders(env) -> None:
    _settings(env)
    lib = _library(env, "carnival")
    _clip(env, lib, "Carnival/BRoll/Phone/p1.jpg", "2024-06-15T14:00:00", offset=-240, gps=(10.65, -61.50),
          device=PHONE)
    _clip(env, lib, "Carnival/BRoll/Phone/p2.jpg", "2024-06-15T15:00:00", offset=-240, gps=(10.65, -61.52),
          device=PHONE)
    between = _clip(env, lib, "Carnival/BRoll/BMC/c1.jpg", "2024-06-15T14:30:00")
    after = _clip(env, lib, "Carnival/BRoll/BMC/c2.jpg", "2024-06-15T17:00:00")
    far = _clip(env, lib, "Carnival/BRoll/BMC/c3.jpg", "2024-06-15T23:30:00")
    no_time = _clip(env, lib, "Carnival/BRoll/BMC/c4.jpg", "2024-06-15T14:30:00")
    _sql_exec(env, "UPDATE assets SET taken_at = NULL WHERE asset_id = :a", a=no_time)
    assert _due(env, lib) == {between, after, far}

    outcome, _ = _run(env, lib)
    assert outcome is None
    loc = _location(env, between)
    assert loc["source"] == "time" and loc["status"] == "applied"
    assert (loc["lat"], loc["lon"]) == (pytest.approx(10.65), pytest.approx(-61.51, abs=1e-4))
    assert loc["basis"]["method"] == "between"
    assert loc["basis"]["summary"] == "From iPhone 15 Pro photos 14:00–15:00"
    nearest = _location(env, after)
    assert nearest["basis"]["method"] == "nearest" and nearest["radius_m"] == 100 + 8000
    assert _location(env, far) is None and _lineage(env, far) == {"producer": "location", "outcome": "empty"}
    assert _due(env, lib) == set()

    client, headers, *_ = env
    detail = client.get(f"/v1/assets/{between}", headers=headers).json()
    assert detail["location"]["source"] == "time"
    assert detail["location"]["basis_summary"] == "From iPhone 15 Pro photos 14:00–15:00"
    # A guess counts only when guesses are asked for.
    q = [("f", f"library:{lib}"), ("f", "has_gps:yes")]
    found = {i["asset_id"] for i in client.get("/v1/query", params=q, headers=headers).json()["items"]}
    assert between not in found
    q.append(("f", "include_guesses:yes"))
    found = {i["asset_id"] for i in client.get("/v1/query", params=q, headers=headers).json()["items"]}
    assert {between, after} <= found
    facets = client.get("/v1/assets/facets", params=[("f", f"library:{lib}")], headers=headers).json()
    assert facets["guess_count"] == 2


def _sql_exec(env, query: str, **params) -> None:
    engine = create_engine(env[-1])
    try:
        with engine.begin() as c:
            c.execute(text(query), params)
    finally:
        engine.dispose()


def test_the_servers_gaps_are_the_producers(env) -> None:
    """The server picks the nearest fixes by the same gap the producer works with."""
    _settings(env)
    lib = _library(env, "gaps")
    cases = [("p1", "2024-06-15T08:00:00", -240), ("p2", "2024-06-15T13:50:00", None),
             ("p3", "2024-06-15T16:10:00", 60), ("p4", "2024-06-15T19:30:00", -240)]
    from src.producers.location.infer import Clock

    fixes = {}
    for name, when, offset in cases:
        a = _clip(env, lib, f"Phone/{name}.jpg", when, offset=offset, gps=(1.0, 1.0), device=PHONE)
        fixes[a] = Stamp(parse_time(f"{when}+00:00"), offset, "Apple|iPhone 15 Pro|")
    a = _clip(env, lib, "Other/same.jpg", "2024-06-15T14:40:00", gps=(1.0, 1.0), device=CAMERA, serial="9")
    fixes[a] = Stamp(parse_time("2024-06-15T14:40:00+00:00"), None, "Blackmagic Design|Pocket Cinema Camera 6K|9")
    for clip_offset in (None, -240, 120):
        c = _clip(env, lib, f"BMC/c{clip_offset}.jpg", "2024-06-15T14:00:00", offset=clip_offset)
        for minutes, clock in ((600, 0.0), (600, 45.0), (240, -90.0), (90, 0.0)):
            ctx = _context(env, c, minutes=minutes, clock=clock)
            me = Stamp(parse_time(ctx["clip"]["taken_at"]), ctx["clip"]["taken_at_offset_min"], ctx["clip"]["device"])
            gaps = {i: gap_minutes(me, s, Clock(clock, 3)) for i, s in fixes.items()}
            befores = sorted((g, i) for i, g in gaps.items() if 0 <= g <= minutes)
            afters = sorted((-g, i) for i, g in gaps.items() if -minutes <= g < 0)
            assert [f["asset_id"] for f in ctx["fixes"]] == [x[1] for x in (befores[:1] + afters[:1])], ctx
            for f in ctx["fixes"]:
                assert f["gap_min"] == pytest.approx(gaps[f["asset_id"]]), (clip_offset, clock, f)


def test_a_persons_location_is_never_overwritten_and_is_a_fix(env) -> None:
    _settings(env)
    client, headers, *_ = env
    lib = _library(env, "person")
    _clip(env, lib, "Phone/p.jpg", "2024-06-15T10:00:00", gps=(20.0, 20.0), device=PHONE)
    placed = _clip(env, lib, "BMC/placed.jpg", "2024-06-15T14:10:00")
    filed = _clip(env, lib, "BMC/filed.jpg", "2024-06-15T14:15:00", gps=(30.0, 30.0))
    near = _clip(env, lib, "BMC/near.jpg", "2024-06-15T14:20:00")
    _person(env, [placed], 1.0, 2.0)
    assert placed not in _due(env, lib) and filed not in _due(env, lib)

    # Saved over by hand, the guess route keeps a person's and the file's.
    guess = {"lat": 5.0, "lon": 5.0, "radius_m": 100, "basis": {}}
    for asset_id in (placed, filed):
        r = client.put(f"/v1/assets/locations/guess/{asset_id}", json={"guess": guess, "minutes": 360,
                                                                          "lineage": made("location")}, headers=headers)
        assert r.status_code == 200 and r.json() == {"result": "kept"}, r.text
        r = client.put(f"/v1/assets/locations/guess/{asset_id}", json={"guess": None, "minutes": 360,
                                                                          "lineage": made("location")}, headers=headers)
        assert r.json() == {"result": "kept"}
    assert _location(env, placed)["source"] == "person" and (_location(env, placed)["lat"]) == 1.0
    assert _lineage(env, placed)["producer"] == "person"
    assert _location(env, filed) is None

    _run(env, lib)
    assert _location(env, placed) == {"lat": 1.0, "lon": 2.0, "radius_m": 0, "source": "person",
                                      "status": "applied", "basis": {}}
    # The nearest fix before the clip beside them is the file's GPS, 5 minutes before.
    assert _location(env, near)["basis"]["fixes"] == [{"asset_id": filed, "gap_min": 5.0}]


def test_a_persons_location_counts_as_a_fix_for_its_neighbours(env) -> None:
    _settings(env)
    lib = _library(env, "person-fix")
    placed = _clip(env, lib, "BMC/placed.jpg", "2024-06-15T14:10:00")
    near = _clip(env, lib, "BMC/near.jpg", "2024-06-15T14:20:00")
    _person(env, [placed], 1.0, 2.0)
    _run(env, lib)
    loc = _location(env, near)
    assert (loc["lat"], loc["lon"]) == (1.0, 2.0) and loc["basis"]["fixes"][0]["asset_id"] == placed
    assert loc["basis"]["summary"] == "From placed clips at 14:10"


def test_guesses_are_never_fixes(env) -> None:
    """A guess one window from a fix doesn't carry a clip a further window on."""
    _settings(env)
    lib = _library(env, "anchors")
    fix = _clip(env, lib, "Phone/fix.jpg", "2024-06-15T08:00:00", gps=(3.0, 4.0), device=PHONE)
    first = _clip(env, lib, "BMC/first.jpg", "2024-06-15T13:00:00")  # 300 min after the fix
    second = _clip(env, lib, "BMC/second.jpg", "2024-06-15T15:00:00")  # 420 min after it, 120 after the guess
    _run(env, lib, only=[first])
    assert _location(env, first)["basis"]["fixes"] == [{"asset_id": fix, "gap_min": 300.0}]
    assert _context(env, second)["fixes"] == []
    _run(env, lib, only=[second])
    assert _location(env, second) is None and _lineage(env, second)["outcome"] == "empty"
    # Nor in the clock's scene pairs: a guessed clip is a clip without a fix.
    client, headers, *_ = env
    r = client.post("/v1/assets/locations/clocks", json={"asset_ids": [second]}, headers=headers)
    assert r.status_code == 200 and all(not d["pairs"] for d in r.json()["days"].values())


def _recheck(env, asset_id: str) -> str | None:
    rows = _sql(env, "SELECT recheck FROM asset_location WHERE asset_id = :a", a=asset_id)
    return rows[0]["recheck"] if rows else None


def _trash(env, asset_id: str) -> None:
    client, headers, *_ = env
    r = client.delete(f"/v1/assets/{asset_id}", params={"remove_from_projects": True}, headers=headers)
    assert r.status_code == 204, r.text


def _restore(env, asset_id: str) -> None:
    client, headers, *_ = env
    r = client.post(f"/v1/assets/{asset_id}/restore", headers=headers)
    assert r.status_code == 204, r.text


def _capture(env, asset_id: str, taken_at: str) -> None:
    client, headers, *_ = env
    r = client.put(f"/v1/assets/{asset_id}/capture", json={"taken_at": taken_at, "lineage": made("capture")},
                   headers=headers)
    assert r.status_code == 200 and r.json() == {"changed": True}, r.text


def _detail(env, asset_id: str) -> dict:
    client, headers, *_ = env
    return client.get(f"/v1/assets/{asset_id}", headers=headers).json()["location"]


def test_the_guesses_around_a_fix_are_checked_again_when_it_changes(env) -> None:
    _settings(env)
    client, headers, *_ = env
    lib = _library(env, "neighbours")
    other_lib = _library(env, "elsewhere")
    a = _clip(env, lib, "Phone/a.jpg", "2024-06-15T10:00:00", gps=(1.0, 1.0), device=PHONE)
    c = _clip(env, lib, "BMC/c.jpg", "2024-06-15T12:00:00")
    days_on = _clip(env, lib, "BMC/later.jpg", "2024-06-18T12:00:00")
    _clip(env, lib, "Phone/later-fix.jpg", "2024-06-18T11:00:00", gps=(9.0, 9.0), device=PHONE)
    elsewhere = _clip(env, other_lib, "BMC/c.jpg", "2024-06-15T12:00:00")
    _clip(env, other_lib, "Phone/a.jpg", "2024-06-15T11:00:00", gps=(7.0, 7.0), device=PHONE)
    _run(env, lib)
    _run(env, other_lib)
    assert _location(env, c)["basis"]["fixes"] == [{"asset_id": a, "gap_min": 120.0}]
    untouched = {x: _location(env, x) for x in (days_on, elsewhere)}

    # A new phone photo near it: due again, its guess kept meanwhile; then a surer one from both sides.
    b = _clip(env, lib, "Phone/b.jpg", "2024-06-15T12:30:00", gps=(1.0, 1.01), device=PHONE)
    assert _location(env, c)["basis"]["fixes"][0]["asset_id"] == a and _lineage(env, c)["outcome"] == "ok"
    assert _recheck(env, c) == "new_fix" and c in _due(env, lib)
    _run(env, lib)
    assert [f["asset_id"] for f in _location(env, c)["basis"]["fixes"]] == [a, b]
    assert _recheck(env, c) is None and c not in _due(env, lib)

    # A person places a clip beside it: a fix, so its neighbours are checked again.
    d = _clip(env, lib, "BMC/d.jpg", "2024-06-15T12:10:00")
    _run(env, lib)
    _person(env, [d], 1.0, 1.005)
    assert c in _due(env, lib) and _lineage(env, d)["producer"] == "person"
    _run(env, lib)
    assert [f["asset_id"] for f in _location(env, c)["basis"]["fixes"]] == [a, d]

    # Cleared: a fix the guess was made from, so it's replaced (less sure), and d itself is guessed.
    r = client.request("DELETE", "/v1/assets/locations", json={"asset_ids": [d]}, headers=headers)
    assert r.status_code == 200, r.text
    assert _recheck(env, c) == "basis_changed" and {c, d} <= _due(env, lib)
    _run(env, lib)
    assert [f["asset_id"] for f in _location(env, c)["basis"]["fixes"]] == [a, b]
    assert _location(env, d)["source"] == "time"

    # The phone photo's time read again from its file: checked again too.
    _capture(env, a, "2024-06-15T11:00:00")
    assert c in _due(env, lib)
    _run(env, lib)
    assert _location(env, c)["basis"]["fixes"][0] == {"asset_id": a, "gap_min": 60.0}

    # Clips days away and in other libraries kept theirs.
    assert {x: _location(env, x) for x in (days_on, elsewhere)} == untouched


def test_a_surer_new_fix_replaces_a_guess_and_a_less_sure_one_doesnt(env) -> None:
    _settings(env)
    lib = _library(env, "surer")
    a = _clip(env, lib, "Phone/a.jpg", "2024-06-15T10:00:00", gps=(1.0, 1.0), device=PHONE)
    sure = _clip(env, lib, "BMC/sure.jpg", "2024-06-15T10:10:00")  # 10 min after a: 767 m
    loose = _clip(env, lib, "BMC/loose.jpg", "2024-06-15T13:00:00")  # 3 h after a: 12.1 km
    _run(env, lib)
    assert _location(env, sure)["radius_m"] == 767 and _location(env, loose)["radius_m"] == 12_100
    assert _location(env, sure)["basis"]["fixes"] == [{"asset_id": a, "gap_min": 10.0}]
    before = _location(env, sure)

    # A phone photo 20 minutes after sure, 0.5° away: both see a guess "between", about 28 km for sure
    # (less sure than it was: kept) and 1.7 km for loose (surer: taken).
    b = _clip(env, lib, "Phone/b.jpg", "2024-06-15T10:30:00", gps=(1.0, 1.5), device=PHONE)
    assert _recheck(env, sure) == "new_fix" and _recheck(env, loose) == "new_fix"
    _run(env, lib)
    assert _location(env, sure) == before and _recheck(env, sure) is None and sure not in _due(env, lib)
    assert _lineage(env, sure) == {"producer": "location", "outcome": "ok"}
    assert _location(env, loose)["basis"]["fixes"] == [{"asset_id": b, "gap_min": 150.0}]
    assert _location(env, loose)["radius_m"] == 100 + 10_000


def test_a_moved_or_retimed_fix_replaces_the_guess_even_less_sure_or_removes_it(env) -> None:
    _settings(env)
    lib = _library(env, "moved")
    a = _clip(env, lib, "Phone/a.jpg", "2024-06-15T10:00:00", gps=(1.0, 1.0), device=PHONE)
    c = _clip(env, lib, "BMC/c.jpg", "2024-06-15T11:00:00")  # 60 min after a: 4,100 m
    _run(env, lib)
    assert _location(env, c)["radius_m"] == 4_100

    # A person corrects a: the guess moves with it.
    _person(env, [a], 2.0, 2.0, replace="all")
    assert _recheck(env, c) == "basis_changed"
    _run(env, lib)
    loc = _location(env, c)
    assert (loc["lat"], loc["lon"], loc["radius_m"]) == (2.0, 2.0, 4_100)

    # a's time read again, 4 hours earlier: less sure, but the old guess was wrong, so it's replaced.
    _capture(env, a, "2024-06-15T07:00:00")
    assert _recheck(env, c) == "basis_changed"
    _run(env, lib)
    assert _location(env, c)["radius_m"] == 100 + 16_000

    # Again, now a day away: nothing within the window, so the guess goes.
    _capture(env, a, "2024-06-14T07:00:00")
    _run(env, lib)
    assert _location(env, c) is None and _lineage(env, c)["outcome"] == "empty"


def test_a_trashed_fix_keeps_a_guess_marked_or_takes_another_and_restoring_rechecks(env) -> None:
    _settings(env)
    lib = _library(env, "trashed")
    a = _clip(env, lib, "Phone/a.jpg", "2024-06-15T10:00:00", gps=(1.0, 1.0), device=PHONE)
    lone = _clip(env, lib, "BMC/lone.jpg", "2024-06-15T12:00:00")
    far = _clip(env, lib, "Phone/far.jpg", "2024-06-16T09:00:00", gps=(5.0, 5.0), device=PHONE)
    near_far = _clip(env, lib, "BMC/near-far.jpg", "2024-06-16T12:10:00")  # 190 min after far
    f2 = _clip(env, lib, "Phone/f2.jpg", "2024-06-16T12:00:00", gps=(5.1, 5.1), device=PHONE)  # 10 min before
    _run(env, lib)
    assert _location(env, near_far)["basis"]["fixes"] == [{"asset_id": f2, "gap_min": 10.0}]
    kept = _location(env, lone)

    # Its only fix trashed: due, and kept where it was, marked.
    _trash(env, a)
    assert _recheck(env, lone) == "basis_gone" and lone in _due(env, lib)
    _run(env, lib)
    loc = _location(env, lone)
    assert {k: loc[k] for k in ("lat", "lon", "radius_m")} == {k: kept[k] for k in ("lat", "lon", "radius_m")}
    assert loc["basis"]["basis_gone"] is True and _recheck(env, lone) is None and lone not in _due(env, lib)
    detail = _detail(env, lone)
    assert detail["basis_gone"] is True and detail["basis_summary"] == "From iPhone 15 Pro photos at 10:00"

    # With another fix in the window: that one's guess, however much less sure.
    _trash(env, f2)
    _run(env, lib)
    loc = _location(env, near_far)
    assert loc["basis"]["fixes"] == [{"asset_id": far, "gap_min": 190.0}] and loc["radius_m"] == 100 + 12_667
    assert "basis_gone" not in loc["basis"]

    # Restored: a new fix again, so the marked guess is checked again and made from it, unmarked.
    _restore(env, a)
    assert _recheck(env, lone) == "new_fix"
    _run(env, lib)
    loc = _location(env, lone)
    assert loc["basis"]["fixes"] == [{"asset_id": a, "gap_min": 120.0}] and "basis_gone" not in loc["basis"]
    assert _detail(env, lone)["basis_gone"] is False
    _restore(env, f2)
    _run(env, lib)
    assert _location(env, near_far)["basis"]["fixes"] == [{"asset_id": f2, "gap_min": 10.0}]


def test_a_new_window_checks_guesses_again_without_removing_them_first(env) -> None:
    _settings(env)
    client, headers, *_ = env
    lib = _library(env, "window")
    a = _clip(env, lib, "Phone/a.jpg", "2024-06-15T08:00:00", gps=(1.0, 1.0), device=PHONE)
    b = _clip(env, lib, "Phone/b.jpg", "2024-06-15T19:00:00", gps=(1.0, 1.01), device=PHONE)
    c = _clip(env, lib, "BMC/c.jpg", "2024-06-15T12:00:00")  # 240 min after a, 420 before b
    early = _clip(env, lib, "BMC/early.jpg", "2024-06-15T01:00:00")  # 420 min before a
    _run(env, lib)
    assert _location(env, c)["basis"]["fixes"] == [{"asset_id": a, "gap_min": 240.0}]
    assert _location(env, early) is None and _lineage(env, early)["outcome"] == "empty"
    guessed_before = _sql(env, "SELECT count(*) AS n FROM asset_location WHERE source = 'time'")[0]["n"]

    # Wider: no question asked, nothing removed; checked again, and the clip where nothing was found is due.
    r = client.put("/v1/producers/location/settings", json={"settings": {"inference_minutes": 480}}, headers=headers)
    assert r.status_code == 200, r.text
    assert _sql(env, "SELECT count(*) AS n FROM asset_location WHERE source = 'time'")[0]["n"] == guessed_before
    assert _recheck(env, c) == "window_changed" and {c, early} <= _due(env, lib)
    _run(env, lib)
    assert [f["asset_id"] for f in _location(env, c)["basis"]["fixes"]] == [a, b]  # surer: between them
    assert _location(env, early)["basis"]["fixes"] == [{"asset_id": a, "gap_min": -420.0}]

    # Narrower, leaving b outside: that guess is wrong now, and replaced (less sure) from a alone.
    r = client.put("/v1/producers/location/settings", json={"settings": {"inference_minutes": 300}}, headers=headers)
    assert r.status_code == 200, r.text
    assert _recheck(env, c) == "basis_changed" and _recheck(env, early) == "basis_changed"
    _run(env, lib)
    assert _location(env, c)["basis"]["fixes"] == [{"asset_id": a, "gap_min": 240.0}]
    assert _location(env, early) is None and _lineage(env, early)["outcome"] == "empty"

    # Narrower again, leaving a outside too: the guess goes.
    r = client.put("/v1/producers/location/settings", json={"settings": {"inference_minutes": 120}}, headers=headers)
    assert r.status_code == 200, r.text
    _run(env, lib)
    assert _location(env, c) is None and _lineage(env, c)["outcome"] == "empty"
    _settings(env)


def test_nothing_else_checks_a_guess_again_and_a_persons_location_is_never_touched(env) -> None:
    _settings(env)
    lib = _library(env, "quiet")
    other = _library(env, "quiet-other")
    a = _clip(env, lib, "Phone/a.jpg", "2024-06-15T10:00:00", gps=(1.0, 1.0), device=PHONE)
    z = _clip(env, lib, "Phone/z.jpg", "2024-06-15T09:00:00", gps=(1.5, 1.5), device=PHONE)
    c = _clip(env, lib, "BMC/c.jpg", "2024-06-15T12:00:00")
    placed = _clip(env, lib, "BMC/placed.jpg", "2024-06-15T11:00:00")
    _person(env, [placed], 3.0, 3.0)
    _run(env, lib)
    person = _sql(env, "SELECT * FROM asset_location WHERE asset_id = :a", a=placed)
    person_lineage = _lineage(env, placed)
    assert _location(env, c)["basis"]["fixes"][0]["asset_id"] == placed  # the nearest fix before it
    guess = _location(env, c)

    def quiet() -> None:
        assert _recheck(env, c) is None and c not in _due(env, lib) and _location(env, c) == guess

    # A clip without a fix near it, added and trashed; a fix in another library; a fix days away; a fix
    # trashed that the guess wasn't made from; capture facts read again unchanged; the same settings.
    x = _clip(env, lib, "BMC/x.jpg", "2024-06-15T12:05:00")
    _run(env, lib, only=[x])
    quiet()
    _trash(env, x)
    quiet()
    _clip(env, other, "Phone/o.jpg", "2024-06-15T12:00:00", gps=(4.0, 4.0), device=PHONE)
    _clip(env, lib, "Phone/later.jpg", "2024-06-19T12:00:00", gps=(4.0, 4.0), device=PHONE)
    quiet()
    _trash(env, z)
    quiet()
    client, headers, *_ = env
    r = client.put(f"/v1/assets/{a}/capture", json={"taken_at": "2024-06-15T10:00:00", "lineage": made("capture")},
                   headers=headers)
    assert r.status_code == 200 and r.json() == {"changed": False}
    quiet()
    _settings(env)
    quiet()

    # Through every trigger, a person's location stays as it was, and is never due.
    _clip(env, lib, "Phone/new.jpg", "2024-06-15T11:30:00", gps=(1.0, 1.0), device=PHONE)
    _trash(env, a)
    _restore(env, a)
    _capture(env, a, "2024-06-15T10:30:00")
    r = client.put("/v1/producers/location/settings", json={"settings": {"inference_minutes": 200}}, headers=headers)
    assert r.status_code == 200, r.text
    _run(env, lib)
    _settings(env)
    _run(env, lib)
    assert _sql(env, "SELECT * FROM asset_location WHERE asset_id = :a", a=placed) == person
    assert _lineage(env, placed) == person_lineage and placed not in _due(env, lib)


def test_the_clock_offset_from_scene_pairs_places_a_camera_on_home_time(env) -> None:
    """The camera's clock is 6 hours ahead (home time). Three of its stills
    look like three phone photos: they vote the offset, and a clip with no
    pair is placed between the phone photos either side of its real time."""
    _settings(env)
    client, headers, *_ = env
    lib = _library(env, "clock")
    rng = random.Random(7)

    def unit() -> list[float]:
        v = [rng.gauss(0, 1) for _ in range(512)]
        n = math.sqrt(sum(x * x for x in v))
        return [x / n for x in v]

    phones, cameras, vectors = [], [], []
    for h, lon in ((9, 30.0), (10, 30.1), (11, 30.2), (12, 30.3)):
        phones.append(_clip(env, lib, f"Phone/p{h}.jpg", f"2024-06-15T{h:02d}:00:00", offset=120,
                            gps=(50.0, lon), device=PHONE))
        vectors.append(unit())
    for h in (15, 16, 17):
        cameras.append(_clip(env, lib, f"Cam/c{h}.jpg", f"2024-06-15T{h:02d}:00:00", device=CAMERA))
    lone = _clip(env, lib, "Cam/lone.jpg", "2024-06-15T17:30:00", device=CAMERA)
    items = [{"asset_id": p, "model_id": "clip", "model_version": "1", "vector": v} for p, v in zip(phones, vectors, strict=True)]
    items += [{"asset_id": c, "model_id": "clip", "model_version": "1", "vector": v} for c, v in zip(cameras, vectors[:3], strict=True)]
    r = client.post("/v1/assets/batch-embeddings", json={"items": items, "lineage": made("clip")}, headers=headers)
    assert r.status_code == 200, r.text

    r = client.post("/v1/assets/locations/clocks", json={"asset_ids": [lone]}, headers=headers)
    [day] = r.json()["days"].values()
    assert sorted(p["fix_id"] for p in day["pairs"]) == sorted(phones[:3])
    assert all(p["similarity"] == pytest.approx(1.0) for p in day["pairs"])

    _run(env, lib)
    loc = _location(env, lone)
    assert loc["basis"]["clock_offset_min"] == 360.0 and loc["basis"]["votes"] == 3
    assert loc["basis"]["method"] == "between"
    assert [f["asset_id"] for f in loc["basis"]["fixes"]] == [phones[2], phones[3]]
    assert loc["lon"] == pytest.approx(30.25, abs=1e-3)
    assert loc["basis"]["summary"].endswith("· clock 6.0 h ahead")


def test_changing_the_window_doesnt_ask_and_turning_it_off_removes_guesses(env) -> None:
    client, headers, *_ = env
    _settings(env)
    lib = _library(env, "toggle")
    _clip(env, lib, "Phone/p.jpg", "2024-06-15T10:00:00", gps=(1.0, 1.0), device=PHONE)
    guessed = _clip(env, lib, "BMC/g.jpg", "2024-06-15T11:00:00")
    placed = _clip(env, lib, "BMC/placed.jpg", "2024-06-15T11:30:00")
    _person(env, [placed], 2.0, 2.0)
    _run(env, lib)
    assert _location(env, guessed)["source"] == "time"

    # The window isn't in lineage: nothing to make again, so nothing asked (guesses are checked again).
    r = client.put("/v1/producers/location/settings", json={"settings": {"inference_minutes": 120}}, headers=headers)
    assert r.status_code == 200, r.text
    assert _location(env, guessed)["source"] == "time"
    _run(env, lib)
    _settings(env)
    r = client.put("/v1/producers/location/settings", json={"settings": {"infer_location": False}}, headers=headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "redo_on_change", r.text
    n = r.json()["error"]["details"]["clips"]
    assert n >= 1 and "remove location guesses" in r.json()["error"]["message"]
    assert _location(env, guessed)["source"] == "time"  # asked, nothing done
    r = client.put("/v1/producers/location/settings", json={"settings": {"infer_location": False}, "redo": True},
                   headers=headers)
    assert r.status_code == 200, r.text
    assert _location(env, guessed) is None and _lineage(env, guessed) is None
    assert _location(env, placed)["source"] == "person" and _lineage(env, placed)["producer"] == "person"
    assert _sql(env, "SELECT count(*) AS n FROM asset_location WHERE source = 'time'") == [{"n": 0}]
    assert _due(env, lib) == set()
    _settings(env)
    assert guessed in _due(env, lib) and placed not in _due(env, lib)


def test_the_guess_route_takes_only_what_a_machine_says_it_made(env) -> None:
    client, headers, *_ = env
    lib = _library(env, "route")
    c = _clip(env, lib, "BMC/c.jpg", "2024-06-15T11:00:00")
    guess = {"lat": 1.0, "lon": 1.0, "radius_m": 100, "basis": {}}
    r = client.put(f"/v1/assets/locations/guess/{c}", json={"guess": guess, "minutes": 360}, headers=headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "lineage_required"
    r = client.put(f"/v1/assets/locations/guess/{c}", json={"guess": {**guess, "lat": 0, "lon": 0}, "minutes": 360,
                                                           "lineage": made("location")}, headers=headers)
    assert r.status_code == 422
    r = client.put(f"/v1/assets/locations/guess/{c}", json={"guess": {**guess, "lat": 91}, "minutes": 360,
                                                           "lineage": made("location")}, headers=headers)
    assert r.status_code == 422
    r = client.put(f"/v1/assets/locations/guess/{c}", json={"guess": guess, "lineage": made("location")},
                   headers=headers)
    assert r.status_code == 422  # the window it was made in, always
    r = client.put("/v1/assets/locations/guess/ast_nope", json={"guess": guess, "minutes": 360,
                                                               "lineage": made("location")}, headers=headers)
    assert r.json() == {"result": "gone"}
    # Made in a window that's no longer the setting: nothing written, tried again.
    _settings(env)
    r = client.put(f"/v1/assets/locations/guess/{c}", json={"guess": guess, "minutes": 120,
                                                           "lineage": made("location")}, headers=headers)
    assert r.json() == {"result": "stale"} and _location(env, c) is None and _lineage(env, c) is None
    r = client.post("/v1/assets/locations/clocks", json={}, headers=headers)
    assert r.status_code == 400  # named clips only, never "all"


def test_only_the_scheduler_reads_for_guesses_or_saves_them(env) -> None:
    """Machine data: admins only (the scheduler's key is an admin's), as #80 made its other routes."""
    from tests.test_archive_trash_safety import _key_with_role

    _settings(env)
    client, *_ = env
    lib = _library(env, "who")
    _clip(env, lib, "Phone/p.jpg", "2024-06-15T10:00:00", gps=(1.0, 1.0), device=PHONE)
    c = _clip(env, lib, "BMC/c.jpg", "2024-06-15T11:00:00")
    guess = {"guess": {"lat": 5.0, "lon": 5.0, "radius_m": 100, "basis": {}}, "minutes": 360,
             "lineage": made("location")}
    for role in ("viewer", "editor"):
        key = _key_with_role(env, role)
        assert client.post("/v1/assets/locations/clocks", json={"asset_ids": [c]}, headers=key).status_code == 403
        assert client.get(f"/v1/assets/locations/context/{c}", headers=key).status_code == 403
        assert client.put(f"/v1/assets/locations/guess/{c}", json=guess, headers=key).status_code == 403, role
    assert _location(env, c) is None and _lineage(env, c) is None
    admin = _key_with_role(env, "admin")
    assert client.get(f"/v1/assets/locations/context/{c}", headers=admin).status_code == 200
    assert client.put(f"/v1/assets/locations/guess/{c}", json=guess, headers=admin).json() == {"result": "stored"}
