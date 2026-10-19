"""The GeoNames plug-in through Lumiverb against the golden file (ADR-023
phase 3, docs/adr/023-phase-3-plan.md section 2): the built-in Place names'
answers to the same questions on the tests' own GeoNames files
(tests/plugins/geonames_lists.py), written into tests/plugins/geonames_golden.json
from that code before part 4 removed it. The plug-in's own answers are held
to it under each python3 by tests/test_geonames_program.py; here, what a
person sees: GET /v1/places finds the places it found, and the Lightbox's
facts say what its place lines said. Whitespace is compared collapsed on
both sides: Lumiverb's checks of a plug-in's answers collapse it (a page
shows it collapsed anyway).

Search finds a clip by both of its facts, the landmark it's at and the town
it's near (Robert, 2026-10-19), clips named before `at` was searched too:
the search sweep writes their text again. Installed off, as Lumiverb installs
its own, it fetches nothing until an account turns it on, and Place search
says why it has no places meanwhile.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from src.producers import INSTALLED, use_added
from src.producers.plugin_work import PluginWork
from src.server.config import get_settings
from src.server.database import get_tenant_session, tenant_database_url
from src.server.plugin_producers import refresh
from tests.clips import make_clip
from tests.plugins import geonames_lists as lists
from tests.producer_fakes import FakeAccount, due, run_job, scheduler_client
from tests.test_first_run_setup import control  # noqa: F401 (a fixture)
from tests.test_geonames_program import GOLDEN
from tests.test_plugin_install import _listed
from tests.test_plugin_runner import _until
from tests.test_plugin_runner import data as short_data  # noqa: F401 (a fixture)
from tests.test_server_admins import install  # noqa: F401 (a fixture)

pytestmark = pytest.mark.slow
GEONAMES = "geonames"
CREDIT = {"text": "GeoNames", "url": "https://www.geonames.org/",
          "license": {"text": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"}}


def _golden() -> dict:
    return json.loads(GOLDEN.read_text())["answers"]


def _collapsed(value):
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        return {k: _collapsed(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_collapsed(v) for v in value]
    return value


def _running(f) -> dict:
    return _listed(f["client"], f["robert"])[GEONAMES]["running"]


@pytest.fixture
def lumiverb(request, install, control, short_data, tmp_path, monkeypatch):  # noqa: F811
    """An install with GeoNames on in the account (as Robert's is; off everywhere, parametrized "off"), installed
    as Lumiverb installs its own, its mirror the tests' files (with what Lumiverb won't show, parametrized "odd"),
    its runner started; a library."""
    from src.server.own_plugins import install_own
    from src.server.plugin_runner import Runner
    from src.server.repository.plugins import PluginRepository
    from src.shared.utils import utcnow

    client, robert, tenant_id = install
    param = getattr(request, "param", "")
    mirror = lists.Mirror(lists.write(tmp_path / "site", odd=param == "odd"))
    monkeypatch.setenv("DATA_DIR", str(short_data))
    get_settings.cache_clear()
    if param != "off":
        with get_tenant_session(tenant_id) as session:
            PluginRepository(session).set(GEONAMES, True, acknowledged_version="3", by="usr_robert", now=utcnow())
            session.commit()
    install_own(control, short_data, environ={"LUMIVERB_GEONAMES_URL": mirror.url}, say=lambda line: None)
    refresh()
    r = client.post("/v1/libraries", json={"name": "Places", "root_path": "/Volumes/places"}, headers=robert)
    assert r.status_code == 200, r.text
    runner = Runner(short_data)
    f = {"client": client, "robert": robert, "tenant_id": tenant_id, "library_id": r.json()["library_id"],
         "runner": runner, "mirror": mirror}
    try:
        if param == "off":
            _until(runner, lambda: _running(f)["state"] == "off")
        else:
            _until(runner, lambda: _running(f)["state"] == "ready" and not _running(f).get("detail"))
        yield f
    finally:
        runner.stop_all()
        use_added((), by=INSTALLED)
        mirror.close()


def _places(f, q: str, limit: int) -> dict:
    r = f["client"].get("/v1/places", params={"q": q, "limit": limit}, headers=f["robert"])
    assert r.status_code == 200, r.text
    assert r.json()["unavailable"] == [], r.json()["unavailable"]
    return r.json()


def _as_golden(place: dict) -> dict:
    """A place GET /v1/places found, as the golden file keeps one."""
    source, _, gid = place["place_id"].partition(":")
    assert (source, place["source"]) == (GEONAMES, GEONAMES)
    return {"geonameid": int(gid), **{k: place[k] for k in ("name", "region", "country", "kind", "matched", "lat",
                                                              "lon", "population")}}


def test_through_lumiverb_the_plug_in_finds_the_places_the_golden_file_has(lumiverb) -> None:
    f = lumiverb
    asked = list(zip(lists.questions()["search"], _golden()["search"], strict=True))
    for (typed, limit), golden in asked:
        if not typed.strip() or len(typed) > 200 or limit > 25:
            continue  # what GET /v1/places doesn't take
        theirs = _places(f, typed, limit)
        assert _collapsed([_as_golden(p) for p in theirs["places"]]) == _collapsed(golden), typed
        assert theirs["credits"] == ([{"source": GEONAMES, **CREDIT}] if golden else []), typed
    assert _places(f, "paris", 10)["credits"] == [{"source": GEONAMES, **CREDIT}]


def _named(f, points: list[tuple[float, float]]) -> list[str]:
    """A clip at each point, named by the plug-in's work through the real runner."""
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
    return clips


def test_through_lumiverb_its_facts_say_what_the_golden_file_says(lumiverb) -> None:
    """The plug-in's At and Near for each clip (its section's facts, its empty line for no town) are the
    Lightbox's "At …" and "Near …" lines from the built-in's answers for the same location."""
    f = lumiverb
    asked = [((lat, lon), golden) for (lat, lon, sure, radius_km), golden
             in zip(lists.questions()["facts"], _golden()["facts"], strict=True) if sure == 0 and radius_km == 25]
    clips = _named(f, [point for point, _ in asked])
    for clip, (point, lines) in zip(clips, asked, strict=True):
        detail = f["client"].get(f"/v1/assets/{clip}", headers=f["robert"]).json()
        assert "place" not in detail, "the built-in's place is gone: the plug-in's section says it"
        section = next(s for s in detail["facts"] if s["plugin_id"] == GEONAMES)
        facts = {fact["key"]: (None if fact["empty"] else fact["text"]) for fact in section["facts"]}
        assert _collapsed({"at": facts.get("at"), "near": facts.get("near")}) == _collapsed(lines), point
        if lines["near"] is None:
            assert {"key": "near", "label": "Near", "text": "Not near a town", "url": None, "empty": True} in section["facts"]


def _found(f, words: str) -> set[str]:
    r = f["client"].get(f"/v1/query?f=query:{words}&f=library:{f['library_id']}", headers=f["robert"])
    assert r.status_code == 200, r.text
    return {item["asset_id"] for item in r.json()["items"]}


def test_search_finds_a_clip_by_the_landmark_it_is_at_and_the_town_it_is_near(lumiverb) -> None:
    """Both of its facts are searched (Robert, 2026-10-19: people search by landmarks more than towns), and a
    clip named before `at` was (version 1 searched `near` alone) is found by its landmark once the search sweep
    has written its text again, nothing made again."""
    from src.server.search.sync import run_search_sync_sweep

    f = lumiverb
    eiffel, town = _named(f, [lists.EIFFEL, lists.PARIS])
    assert _found(f, "eiffel") == {eiffel}, "At Tour Eiffel"
    assert _found(f, "paris") == {eiffel, town}, "both near Paris"
    with get_tenant_session(f["tenant_id"]) as session:  # as version 1 saved it: its near alone
        session.execute(text("UPDATE plugin_outputs SET search_text = outputs->'near'->>'text'"
                             " WHERE asset_id = :a"), {"a": eiffel})
        session.commit()
    assert _found(f, "eiffel") == set()
    with get_tenant_session(f["tenant_id"]) as session:
        assert run_search_sync_sweep(session)["facts_synced"] == 1
        assert run_search_sync_sweep(session)["facts_synced"] == 0, "once"
    assert _found(f, "eiffel") == {eiffel}
    assert due(tenant_database_url(f["tenant_id"]), "plugin_geonames", [f["library_id"]]) == [], "nothing made again"


@pytest.mark.parametrize("lumiverb", ["off"], indirect=True, ids=["off"])
def test_on_in_no_account_it_fetches_nothing_until_one_turns_it_on_and_place_says_why(lumiverb) -> None:
    """Installed off by default, as Lumiverb installs its own: not run, nothing fetched (its acknowledgment is
    accepted first), and GET /v1/places says why there are no places, naming it; turned on, it fetches and
    answers."""
    f = lumiverb
    for _ in range(10):
        f["runner"].turn()
    assert f["mirror"].asked == [], "nothing fetched while no account has it on"
    r = f["client"].get("/v1/places", params={"q": "paris"}, headers=f["robert"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "plugin_off", r.text
    assert r.json()["error"]["details"] == {"plugins": [{"plugin_id": GEONAMES, "title": "Place names (GeoNames)"}]}
    assert "Place names (GeoNames)" in r.json()["error"]["message"]
    r = f["client"].put(f"/v1/plugins/{GEONAMES}", json={"enabled": True, "acknowledged": "3"}, headers=f["robert"])
    assert r.status_code == 200, r.text
    _until(f["runner"], lambda: _running(f)["state"] == "ready" and not _running(f).get("detail"))
    assert f["mirror"].gets("cities1000.zip") == 1
    assert [p["name"] for p in _places(f, "paris", 2)["places"]] == ["Paris", "Paris"]


@pytest.mark.parametrize("lumiverb", ["odd"], indirect=True, ids=["odd"])
def test_through_lumiverb_names_it_wont_show_still_answer_a_search(lumiverb) -> None:
    """Names with control characters, < and >, a name longer than Lumiverb takes, and points that aren't
    real: the plug-in's search over them still answers (Lumiverb refuses a whole answer with one bad place)."""
    f = lumiverb
    for typed in ("bad", "bell", "llanfair", "tower", "nowhere", "a", "t"):
        _places(f, typed, 25)
    found = _places(f, "tower", 10)["places"]
    assert [p["label"] for p in found] == ["Tower iof/i Tags, Gizascript, Longland"]
