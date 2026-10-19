"""The GeoNames plug-in against today's Place names on the same files
(ADR-023 phase 3, docs/adr/023-phase-3-plan.md section 2): today's fetch
puts the tests' own GeoNames files (tests/plugins/geonames_lists.py) in a
real Postgres, the plug-in builds its list from them, and both are asked
the same questions, compared answer by answer.

Today's answers are the golden file's (tests/plugins/geonames_golden.json,
which holds the plug-in to them once today's code is gone: written again by
tests/plugins/geonames_golden.py while it exists). The plug-in's are
today's: from its list's own queries, over its socket, and through
Lumiverb: GET /v1/places finds the same places once the plug-in answers as
the built-in's list did standing in for it (part 3's fallback), and the
Lightbox's facts say what the place lines said from the built-in's answers.
Through Lumiverb, whitespace is compared collapsed on both sides: Lumiverb's
checks of a plug-in's answers collapse it (a page shows it collapsed
anyway).
"""

from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import text

from src.plugins.geonames import data as geonames_data
from src.producers import INSTALLED, use_added
from src.producers.plugin_work import PluginWork
from src.server.config import get_settings
from src.server.database import get_control_session, get_tenant_session, tenant_database_url
from src.server.plugin_producers import refresh
from src.server.repository.places import PlaceRepository, label
from tests.clips import make_clip
from tests.plugins import geonames_lists as lists
from tests.producer_fakes import FakeAccount, due, run_job, scheduler_client
from tests.test_first_run_setup import control  # noqa: F401 (a fixture)
from tests.test_geonames_program import FIRST, GOLDEN, GeoNames, as_today
from tests.test_plugin_install import _listed
from tests.test_plugin_runner import (
    _until,
    data,  # noqa: F401 (a fixture)
)
from tests.test_plugin_runner import data as short_data  # noqa: F401 (a fixture)
from tests.test_server_admins import install  # noqa: F401 (a fixture)

pytestmark = pytest.mark.slow
GEONAMES = "geonames"
CREDIT = {"text": "GeoNames", "url": "https://www.geonames.org/",
          "license": {"text": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"}}
WRITE = "LUMIVERB_WRITE_GEONAMES_GOLDEN"  # set by tests/plugins/geonames_golden.py: the golden file written again
ABOUT = ("Today's Place names' answers (src/server/repository/places.py over src/plugins/geonames/data.py's fetch, "
         "in Postgres) to tests/plugins/geonames_lists.py's questions on its files: the GeoNames plug-in is held to "
         "them. Written by tests/plugins/geonames_golden.py.")


@pytest.fixture
def today(control, tmp_path, monkeypatch):  # noqa: F811
    """Today's Place names' list in the control database, fetched from a mirror of the tests' files: the
    mirror."""
    mirror = lists.Mirror(lists.write(tmp_path / "site"))
    monkeypatch.setattr(get_settings(), "lumiverb_geonames_url", mirror.url)
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path / "lumiverb"))
    try:
        geonames_data.fetch(lambda *a, **k: None)
        yield mirror
    finally:
        mirror.close()


def _todays() -> dict:
    with get_control_session() as session:
        return lists.answers(PlaceRepository(session), label)


def test_todays_answers_are_the_golden_files(today) -> None:
    mine = _todays()
    if os.environ.get(WRITE):
        GOLDEN.write_text(json.dumps({"about": ABOUT, "answers": mine}, indent=1, ensure_ascii=False) + "\n")
    golden = json.loads(GOLDEN.read_text())
    assert lists.differences(golden["answers"], mine) == []


def test_the_plug_in_answers_as_today_on_the_same_files(today, data) -> None:  # noqa: F811
    g = GeoNames(data, FIRST, today.url)
    try:
        g.until_ready()
        as_today(g, FIRST, _todays())
    finally:
        g.stop()


# ---------------------------------------------------------------------------
# Through Lumiverb: the built-in's list standing in, then the plug-in
# ---------------------------------------------------------------------------


def _collapsed(value):
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        return {k: _collapsed(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_collapsed(v) for v in value]
    return value


@pytest.fixture
def lumiverb(request, install, control, short_data, tmp_path, monkeypatch):  # noqa: F811
    """An install with GeoNames on in the account (as Robert's is) and the built-in's list fetched into the
    control database from a mirror of the tests' files (with what Lumiverb won't show, parametrized True), the
    plug-in installed from the same mirror (its runner started by the test); a library."""
    from src.server.own_plugins import install_own
    from src.server.repository.plugins import PluginRepository
    from src.shared.utils import utcnow

    client, robert, tenant_id = install
    odd = getattr(request, "param", False)
    mirror = lists.Mirror(lists.write(tmp_path / "site", odd=odd))
    monkeypatch.setenv("DATA_DIR", str(short_data))
    monkeypatch.setenv("LUMIVERB_GEONAMES_URL", mirror.url)
    get_settings.cache_clear()
    if not odd:  # the built-in's fetch can't keep a NUL (Postgres text): only the plug-in is asked of those
        geonames_data.fetch(lambda *a, **k: None)
    with get_tenant_session(tenant_id) as session:
        PluginRepository(session).set(GEONAMES, True, acknowledged_version="3", by="usr_robert", now=utcnow())
        session.commit()
    install_own(control, short_data, geonames_url=mirror.url, say=lambda line: None)
    refresh()
    r = client.post("/v1/libraries", json={"name": "Places", "root_path": "/Volumes/places"}, headers=robert)
    assert r.status_code == 200, r.text
    f = {"client": client, "robert": robert, "tenant_id": tenant_id, "library_id": r.json()["library_id"],
         "data": short_data, "runner": None}
    try:
        yield f
    finally:
        if f["runner"] is not None:
            f["runner"].stop_all()
        use_added((), by=INSTALLED)
        mirror.close()


def _running(f) -> None:
    """The plug-in runner started, until GeoNames answers from its own list."""
    from src.server.plugin_runner import Runner

    f["runner"] = runner = Runner(f["data"])
    _until(runner, lambda: _listed(f["client"], f["robert"])[GEONAMES]["running"]["state"] == "ready"
           and not _listed(f["client"], f["robert"])[GEONAMES]["running"].get("detail"))


def _places(f, q: str, limit: int) -> dict:
    r = f["client"].get("/v1/places", params={"q": q, "limit": limit}, headers=f["robert"])
    assert r.status_code == 200, r.text
    assert r.json()["unavailable"] == [], r.json()["unavailable"]
    return r.json()


def test_through_lumiverb_the_plug_in_finds_the_places_the_built_ins_list_found(lumiverb) -> None:
    f = lumiverb
    asked = [(typed, min(limit, 25)) for typed, limit in lists.questions()["search"] if typed.strip() and len(typed) <= 200]
    standing_in = {a: _places(f, *a) for a in asked}  # the plug-in not running: the built-in's list answers
    _running(f)
    for a in asked:
        theirs = _places(f, *a)
        assert _collapsed(theirs["places"]) == _collapsed(standing_in[a]["places"]), a
        assert theirs["credits"] == standing_in[a]["credits"], a
    assert _places(f, "paris", 10)["credits"] == [{"source": GEONAMES, **CREDIT}]


def test_through_lumiverb_its_facts_say_what_the_place_lines_said(lumiverb) -> None:
    """The plug-in's At and Near for each clip (its section's facts, its empty line for no town) are the
    Lightbox's "At …" and "Near …" lines from the built-in's answers for the same location."""
    f = lumiverb
    _running(f)
    points = [(lat, lon) for lat, lon, sure, radius_km in lists.questions()["facts"] if sure == 0 and radius_km == 25]
    with get_tenant_session(f["tenant_id"]) as session:
        clips = [make_clip(session, f["library_id"], f"{n}.jpg") for n in range(len(points))]
        for clip, (lat, lon) in zip(clips, points, strict=True):
            session.execute(text("UPDATE assets SET gps_lat = :lat, gps_lon = :lon WHERE asset_id = :a"),
                            {"lat": lat, "lon": lon, "a": clip})
        session.commit()
    acct = FakeAccount(scheduler_client(f["tenant_id"]))
    acct.tenant_id = f["tenant_id"]
    items = due(tenant_database_url(f["tenant_id"]), "plugin_geonames", [f["library_id"]])
    run_job(PluginWork, acct, f["tenant_id"], "plugin_geonames", items)
    assert acct.failures.charged == []
    with get_control_session() as session:
        places = PlaceRepository(session)
        said = [lists.at_and_near(places.nearest(lat, lon, 25_000), places.landmark_at(lat, lon, 0), label)
                for lat, lon in points]
    for clip, point, lines in zip(clips, points, said, strict=True):
        detail = f["client"].get(f"/v1/assets/{clip}", headers=f["robert"]).json()
        section = next(s for s in detail["facts"] if s["plugin_id"] == GEONAMES)
        facts = {fact["key"]: (None if fact["empty"] else fact["text"]) for fact in section["facts"]}
        assert _collapsed({"at": facts.get("at"), "near": facts.get("near")}) == _collapsed(lines), point
        if lines["near"] is None:
            assert {"key": "near", "label": "Near", "text": "Not near a town", "url": None, "empty": True} in section["facts"]


@pytest.mark.parametrize("lumiverb", [True], indirect=True, ids=["odd"])
def test_through_lumiverb_names_it_wont_show_still_answer_a_search(lumiverb) -> None:
    """Names with control characters, < and >, a name longer than Lumiverb takes, and points that aren't
    real: the plug-in's search over them still answers (Lumiverb refuses a whole answer with one bad place)."""
    f = lumiverb
    _running(f)
    for typed in ("bad", "bell", "llanfair", "tower", "nowhere", "a", "t"):
        _places(f, typed, 25)
    found = _places(f, "tower", 10)["places"]
    assert [p["label"] for p in found] == ["Tower iof/i Tags, Gizascript, Longland"]
