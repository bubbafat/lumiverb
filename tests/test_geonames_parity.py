"""The GeoNames plug-in against today's Place names on the same files
(ADR-023 phase 3, docs/adr/023-phase-3-plan.md section 2): today's fetch
puts the tests' own GeoNames files (tests/plugins/geonames_lists.py) in a
real Postgres, the plug-in builds its list from them, and both are asked
the same questions, compared answer by answer.

Today's answers are the golden file's (tests/plugins/geonames_golden.json,
which holds the plug-in to them once today's code is gone: written again by
tests/plugins/geonames_golden.py while it exists). The plug-in's are
today's: from its list's own queries, over its socket, and through
Lumiverb, installed beside the built-in Place names under another id: GET
/v1/places finds the same places, and the Lightbox's facts say what its
place lines said. Through Lumiverb, whitespace is compared collapsed on
both sides: Lumiverb's checks of a plug-in's answers collapse it (a page
shows it collapsed anyway).
"""

from __future__ import annotations

import json
import os
import shutil

import pytest
from sqlalchemy import text

from src.plugins.geonames import data as geonames_data
from src.producers import INSTALLED, use_added
from src.producers.place.work import Name
from src.producers.plugin_work import PluginWork
from src.server.config import get_settings
from src.server.database import get_control_session, get_tenant_session, tenant_database_url
from src.server.lifecycle import cli
from src.server.plugin_producers import refresh
from src.server.repository.places import PlaceRepository, label
from tests.clips import make_clip
from tests.plugins import geonames_lists as lists
from tests.producer_fakes import FakeAccount, due, run_job, scheduler_client
from tests.test_first_run_setup import _env_file, control  # noqa: F401 (control: a fixture)
from tests.test_geonames_program import FIRST, GOLDEN, PROGRAM, GeoNames, as_today
from tests.test_plugin_install import _listed
from tests.test_plugin_runner import (
    _until,
    data,  # noqa: F401 (a fixture)
)
from tests.test_plugin_runner import data as short_data  # noqa: F401 (a fixture)
from tests.test_server_admins import install  # noqa: F401 (a fixture)

pytestmark = pytest.mark.slow
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
# Through Lumiverb: the plug-in installed beside the built-in Place names
# ---------------------------------------------------------------------------

PLUGIN = "geonamesplugin"  # the built-in's id is geonames until part 3


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
    """An install with the plug-in installed on (another id) and running, from a mirror of the tests' files
    (with what Lumiverb won't show, when it's parametrized True), the built-in Place names fetching from it
    too once an account turns it on; a library."""
    from src.server.plugin_runner import Runner

    client, robert, tenant_id = install
    mirror = lists.Mirror(lists.write(tmp_path / "site", odd=getattr(request, "param", False)))
    env = _env_file(tmp_path, control)
    env.write_text(env.read_text() + f"DATA_DIR={short_data}\n")
    monkeypatch.setenv("DATA_DIR", str(short_data))
    monkeypatch.setenv("LUMIVERB_GEONAMES_URL", mirror.url)  # the built-in's, read again with the env file's
    get_settings.cache_clear()
    copy = tmp_path / "program"
    shutil.copytree(PROGRAM, copy)
    manifest = json.loads((copy / "lumiverb-plugin.json").read_text())
    manifest.update(id=PLUGIN, title="Place names (the GeoNames plug-in)")
    (copy / "lumiverb-plugin.json").write_text(json.dumps(manifest))
    assert cli.main(["plugins", "install", str(copy), "--on", "--yes", "--env", str(env)]) == 0
    r = client.put(f"/v1/plugins/{PLUGIN}/server-settings", json={"settings": {"mirror": mirror.url}}, headers=robert)
    assert r.status_code == 200, r.text
    refresh()
    runner = Runner(short_data)
    try:
        _until(runner, lambda: _listed(client, robert)[PLUGIN]["running"]["state"] == "ready"
               and not _listed(client, robert)[PLUGIN]["running"].get("detail"))
        r = client.post("/v1/libraries", json={"name": "Places", "root_path": "/Volumes/places"}, headers=robert)
        assert r.status_code == 200, r.text
        yield {"client": client, "robert": robert, "tenant_id": tenant_id, "library_id": r.json()["library_id"],
               "runner": runner}
    finally:
        runner.stop_all()
        use_added((), by=INSTALLED)
        mirror.close()


def _builtin_on(f) -> None:
    """Today's Place names turned on in the account (its acknowledgment accepted), its list fetched."""
    from src.plugins import registry

    version = registry()["geonames"].acknowledgment.version
    r = f["client"].put("/v1/plugins/geonames", json={"enabled": True, "acknowledged": version}, headers=f["robert"])
    assert r.status_code == 200, r.text
    _until(f["runner"], lambda: _listed(f["client"], f["robert"])["geonames"]["data"]["state"] == "ready")


def _places(f, q: str, limit: int) -> dict[str, list]:
    r = f["client"].get("/v1/places", params={"q": q, "limit": limit}, headers=f["robert"])
    assert r.status_code == 200, r.text
    assert r.json()["unavailable"] == [], r.json()["unavailable"]
    by: dict[str, list] = {"geonames": [], PLUGIN: []}
    for p in r.json()["places"]:
        by[p["source"]].append({**{k: p[k] for k in ("name", "region", "country", "label", "lat", "lon", "population",
                                                     "kind", "matched")},
                                "id": p["place_id"].split(":", 1)[1]})
    return by


def test_through_lumiverb_it_finds_the_places_place_names_finds(lumiverb) -> None:
    f = lumiverb
    _builtin_on(f)
    for typed, limit in lists.questions()["search"]:
        if not typed.strip() or len(typed) > 200:
            continue  # GET /v1/places asks for something typed
        found = _places(f, typed, min(limit, 25))
        assert _collapsed(found[PLUGIN]) == _collapsed(found["geonames"]), typed
    r = f["client"].get("/v1/places", params={"q": "paris"}, headers=f["robert"])
    credits = {c["source"]: c for c in r.json()["credits"]}
    assert credits[PLUGIN]["license"] == credits["geonames"]["license"] == {
        "text": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"}


def _job(f, kind: str, work) -> None:
    acct = FakeAccount(scheduler_client(f["tenant_id"]))
    acct.tenant_id = f["tenant_id"]
    items = due(tenant_database_url(f["tenant_id"]), kind, [f["library_id"]])
    assert items, f"nothing of {kind} is due"
    run_job(work, acct, f["tenant_id"], kind, items)
    assert acct.failures.charged == [], acct.failures.charged


def test_through_lumiverb_its_facts_say_what_the_place_lines_said(lumiverb) -> None:
    """The same clips named by both: the plug-in's At and Near (its section's facts, its empty line for no town)
    are the Lightbox's "At …" and "Near …" lines from today's place."""
    f = lumiverb
    _builtin_on(f)
    points = [(lat, lon) for lat, lon, sure, radius_km in lists.questions()["facts"] if sure == 0 and radius_km == 25]
    with get_tenant_session(f["tenant_id"]) as session:
        clips = [make_clip(session, f["library_id"], f"{n}.jpg") for n in range(len(points))]
        for clip, (lat, lon) in zip(clips, points, strict=True):
            session.execute(text("UPDATE assets SET gps_lat = :lat, gps_lon = :lon WHERE asset_id = :a"),
                            {"lat": lat, "lon": lon, "a": clip})
        session.commit()
    _job(f, "place", Name)
    _job(f, f"plugin_{PLUGIN}", PluginWork)
    for clip, point in zip(clips, points, strict=True):
        detail = f["client"].get(f"/v1/assets/{clip}", headers=f["robert"]).json()
        place = detail["place"] or {}
        landmark = place.get("landmark")
        said = {"at": (landmark["name"] if place.get("label") else landmark["label"]) if landmark else None,
                "near": place.get("label")}
        section = next(s for s in detail["facts"] if s["plugin_id"] == PLUGIN)
        facts = {fact["key"]: (None if fact["empty"] else fact["text"]) for fact in section["facts"]}
        assert _collapsed({"at": facts.get("at"), "near": facts.get("near")}) == _collapsed(said), point
        empty = next((fact for fact in section["facts"] if fact["key"] == "near" and fact["empty"]), None)
        if said["near"] is None:
            assert empty is not None and empty["text"] == "Not near a town"


@pytest.mark.parametrize("lumiverb", [True], indirect=True, ids=["odd"])
def test_through_lumiverb_names_it_wont_show_still_answer_a_search(lumiverb) -> None:
    """Names with control characters, < and >, a name longer than Lumiverb takes, and points that aren't
    real: the plug-in's search over them still answers (Lumiverb refuses a whole answer with one bad place)."""
    f = lumiverb
    for typed in ("bad", "bell", "llanfair", "tower", "nowhere", "a", "t"):
        r = f["client"].get("/v1/places", params={"q": typed, "limit": 25}, headers=f["robert"])
        assert r.status_code == 200 and r.json()["unavailable"] == [], (typed, r.text)
    found = f["client"].get("/v1/places", params={"q": "tower"}, headers=f["robert"]).json()["places"]
    assert [p["label"] for p in found] == ["Tower iof/i Tags, Gizascript, Longland"]
