"""Place names after the switch-over (ADR-023 phase 3 part 3), against the
real API and database: GeoNames is an installed plug-in (Lumiverb's own,
src/own_plugins/geonames/), and until part 4 what the built-in Place names
left stands in for it in an account where it's on by the account's own
switch: each clip's old name in the detail until the plug-in's is saved, and
the list it fetched answering place searches and setting a location by place
while the plug-in can't (not installed, or fetching its first list). On in no
account, the plug-in fetches nothing; the first account turning it on starts
its fetch. The old producer's routes are gone (410), and a place a location
was set from keeps its credit whatever becomes of the plug-in. Local web
servers stand in for download.geonames.org (tests/plugins/geonames_lists.py).

Today's files are read as GeoNames writes them (the old fetch's parsers,
used by the parity tests until part 4)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import text

from src.plugins.geonames import data as geonames_data
from src.plugins.geonames.data import (
    parse_countries,
    parse_landmarks,
    parse_names,
    parse_places,
    parse_regions,
    wikipedia_linked,
)
from src.producers import INSTALLED, use_added
from src.producers.plugin_work import PluginWork
from src.server.config import get_settings
from src.server.database import get_control_session, get_tenant_session, tenant_database_url
from src.server.lifecycle import cli
from src.server.own_plugins import install_own
from src.server.plugin_producers import refresh
from src.server.repository.asset_places import still_shown
from src.server.repository.places import PlaceRepository, fold, label
from src.server.repository.plugins import PluginRepository
from src.shared.utils import utcnow
from tests.clips import make_clip
from tests.plugins import geonames_lists as lists
from tests.producer_fakes import FakeAccount, due, run_job, scheduler_client
from tests.test_first_run_setup import _env_file, control  # noqa: F401 (control: a fixture)
from tests.test_plugin_install import _listed
from tests.test_plugin_runner import _until
from tests.test_plugin_runner import data as short_data  # noqa: F401 (a fixture)
from tests.test_server_admins import install  # noqa: F401 (a fixture)

pytestmark = pytest.mark.slow
GEONAMES = "geonames"
CREDIT = {"text": "GeoNames", "url": "https://www.geonames.org/",
          "license": {"text": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"}}


def _place(gid, name, ascii_name, lat, lon, cc, admin1, population, code="PPL"):
    cols = [str(gid), name, ascii_name, "", str(lat), str(lon), "P", code, cc, "", admin1, "", "", "",
            str(population), "", "10", "Etc/UTC", "2026-01-01"]
    return "\t".join(cols)


PLACES = [
    _place(3573890, "Port of Spain", "Port of Spain", 10.66668, -61.51889, "TT", "05", 49031, "PPLC"),
    _place(2988507, "Paris", "Paris", 48.85341, 2.3488, "FR", "11", 2138551, "PPLC"),
    _place(4717560, "Paris", "Paris", 33.66094, -95.55551, "US", "TX", 24171),
    _place(2657896, "Zürich", "Zuerich", 47.36667, 8.55, "CH", "ZH", 415367, "PPLA"),  # GeoNames' own spellings
    _place(3448439, "São Paulo", "Sao Paulo", -23.5475, -46.63611, "BR", "27", 10021295, "PPLA"),
    _place(9999991, "Parisot", "Parisot", 44.26, 1.85, "FR", "76", 1100),
    _place(2294768, "Sampa", "Sampa", 8.0, -2.7, "GH", "04", 20000),  # São Paulo is "Sampa" too
    "not a place line",
]
REGIONS = "TT.05\tPort of Spain\tPort of Spain\t3573891\nFR.11\tÎle-de-France\tIle-de-France\t3012874\n" \
          "US.TX\tTexas\tTexas\t4736286\nCH.ZH\tZurich\tZurich\t2657895\nFR.76\tOccitanie\tOccitanie\t11071623\n" \
          "BR.27\tSão Paulo\tSao Paulo\t3448433\nUS.FL\tFlorida\tFlorida\t4155751\n"


def _feature(gid, name, lat, lon, cls, code, cc, admin1):
    cols = [str(gid), name, name, "", str(lat), str(lon), cls, code, cc, "", admin1, "", "", "", "0", "", "30",
            "Etc/UTC", "2026-01-01"]
    return "\t".join(cols)


MAGIC_KINGDOM = (28.4187, -81.5812)
EIFFEL = (48.8583, 2.2945)
CHAMP_DE_MARS = (48.8561, 2.2980)
FEATURES = [
    _feature(4164161, "Magic Kingdom, Walt Disney World", *MAGIC_KINGDOM, "L", "AMUS", "US", "FL"),  # kept whole
    _feature(6254976, "Tour Eiffel", *EIFFEL, "S", "MNMT", "FR", "11"),  # a Wikipedia article
    _feature(6618997, "Champ de Mars", *CHAMP_DE_MARS, "L", "PRK", "FR", "11"),  # one too
    _feature(9999994, "Eiffel Peak", 46.3, -122.4, "T", "PK", "US", "WA"),  # an article, no other names
    _feature(9999992, "Hôtel du Coin", 48.8584, 2.2946, "S", "HTL", "FR", "11"),  # a kind kept, no article
    _feature(9999993, "Ruisseau", 48.86, 2.30, "H", "STM", "FR", "11"),  # an article, not a kind kept
    _feature(2988507, "Paris", 48.85341, 2.3488, "P", "PPLC", "FR", "11"),  # a town: the towns' list's
    "not a feature line",
]
LANDMARKS = 4  # of FEATURES
ALTERNATE_NAMES = "\n".join([
    "0\t6254976\tfr\tEiffel Tower\t\t\t\t\t\t",  # the same name as its English one, listed first
    "1\t6254976\tlink\thttps://en.wikipedia.org/wiki/Eiffel_Tower\t\t\t\t\t\t",
    "2\t6254976\ten\tEiffel Tower\t1\t\t\t\t\t",
    "3\t6618997\tlink\thttps://fr.wikipedia.org/wiki/Champ-de-Mars\t\t\t\t\t\t",
    "4\t9999992\tlink\thttps://example.com/hotel\t\t\t\t\t\t",  # a link, not Wikipedia's
    "5\t9999993\tlink\thttps://fr.wikipedia.org/wiki/Ruisseau\t\t\t\t\t\t",
    "14\t9999994\tlink\thttps://en.wikipedia.org/wiki/Eiffel_Peak\t\t\t\t\t\t",
    "6\t6254976\tde\tEiffelturm\t\t\t\t\t\t",
    "7\t2657896\tit\tZurigo\t\t\t\t\t\t",
    "8\t2657896\tde\tZürich\t1\t\t\t\t\t",  # its own name, folded: not again
    "9\t2657896\tla\tTuricum\t\t\t\t1\t\t",  # historic
    "10\t2657896\ten\tZurchy\t\t\t1\t\t\t",  # colloquial
    "11\t2657896\tpost\t8001\t\t\t\t\t\t",  # a code, not a name
    "12\t3448439\t\tSampa\t\t\t\t\t\t",  # no language: a name all the same
    "13\t9999993\tfr\tLe Ruisseau\t\t\t\t\t\t",  # not a place kept
    "not a name line",
]) + "\n"
COUNT = 7 + LANDMARKS  # towns and landmarks
COUNTRIES = "#ISO\tISO3\tISO-Numeric\tfips\tCountry\n" \
            "TT\tTTO\t780\tTD\tTrinidad and Tobago\nFR\tFRA\t250\tFR\tFrance\nUS\tUSA\t840\tUS\tUnited States\n" \
            "CH\tCHE\t756\tSZ\tSwitzerland\nBR\tBRA\t076\tBR\tBrazil\n"


def test_the_files_are_read_as_geonames_writes_them() -> None:
    places = list(parse_places(iter(line + "\n" for line in PLACES)))
    assert [p["geonameid"] for p in places] == [3573890, 2988507, 4717560, 2657896, 3448439, 9999991, 2294768]
    zurich = places[3]
    assert (zurich["name"], zurich["ascii_name"], zurich["admin1_code"], zurich["population"]) == (
        "Zürich", "Zuerich", "ZH", 415367)
    assert {r["code"]: r["name"] for r in parse_regions(REGIONS)}["FR.11"] == "Île-de-France"
    assert {c["iso"]: c["name"] for c in parse_countries(COUNTRIES)} == {
        "TT": "Trinidad and Tobago", "FR": "France", "US": "United States", "CH": "Switzerland", "BR": "Brazil"}
    linked = wikipedia_linked(iter(ALTERNATE_NAMES.splitlines(keepends=True)))
    assert linked == {6254976, 6618997, 9999993, 9999994}
    landmarks = list(parse_landmarks(iter(line + "\n" for line in FEATURES), linked))
    assert [(p["name"], p["feature_code"]) for p in landmarks] == [
        ("Magic Kingdom, Walt Disney World", "AMUS"), ("Tour Eiffel", "MNMT"), ("Champ de Mars", "PRK"),
        ("Eiffel Peak", "PK")]
    own = {p["geonameid"]: (fold(p["name"]), p["ascii_name"].lower())
           for p in (*parse_places(iter(line + "\n" for line in PLACES)), *landmarks)}
    names = list(parse_names(iter(ALTERNATE_NAMES.splitlines(keepends=True)), own, fold))
    assert [(n["geonameid"], n["name"], n["language"]) for n in names] == [
        (6254976, "Eiffel Tower", "fr"), (6254976, "Eiffel Tower", "en"), (6254976, "Eiffelturm", "de"),
        (2657896, "Zurigo", "it"), (3448439, "Sampa", "")]
    assert names[0]["folded"] == "eiffel tower"
    assert landmarks[1] == {"geonameid": 6254976, "name": "Tour Eiffel", "ascii_name": "Tour Eiffel",
                            "lat": EIFFEL[0], "lon": EIFFEL[1], "feature_code": "MNMT", "country_code": "FR",
                            "admin1_code": "11"}





# ---------------------------------------------------------------------------
# An account as the built-in Place names left it, then the switch-over
# ---------------------------------------------------------------------------


def _named_as_before(tenant_id: str, asset_id: str, lat: float, lon: float) -> None:
    """The clip's old name, as the built-in producer kept it (asset_place): the nearest town within 25 km and
    the landmark it's at, from the built-in's list."""
    with get_control_session() as session:
        places = PlaceRepository(session)
        town, mark = places.nearest(lat, lon, 25_000), places.landmark_at(lat, lon, 0)
    with get_tenant_session(tenant_id) as session:
        session.execute(text(
            "INSERT INTO asset_place (asset_id, place_id, label, country_code, distance_m, lat, lon, named_at,"
            "   landmark_id, landmark_name, landmark_label, landmark_distance_m)"
            " VALUES (:a, :p, :label, :cc, :d, :lat, :lon, now(), :lm, :lm_name, :lm_label, :lm_d)"
        ), {"a": asset_id, "p": f"geonames:{town['geonameid']}" if town else None, "label": label(town) if town else None,
            "cc": town["country_code"] if town else None, "d": round(town["distance_m"]) if town else None,
            "lat": lat, "lon": lon, "lm": f"geonames:{mark['geonameid']}" if mark else None,
            "lm_name": mark["name"] if mark else None, "lm_label": label(mark) if mark else None,
            "lm_d": round(mark["distance_m"]) if mark else None})
        session.commit()


def _turned_on(tenant_id: str, on: bool = True, version: str = "3") -> None:
    """The account's own switch for GeoNames, as an admin left it (accepting that version)."""
    with get_tenant_session(tenant_id) as session:
        repo = PluginRepository(session)
        repo.set(GEONAMES, on, acknowledged_version=version, by="usr_robert", now=utcnow())
        if on:
            repo.acknowledge(GEONAMES, version, user_id="usr_robert", now=utcnow())
        session.commit()


POINTS = {"tower": lists.EIFFEL, "town": lists.PARIS, "park": lists.MAGIC_KINGDOM, "sea": (0.0, -150.0)}


@pytest.fixture
def account(install, control, short_data, tmp_path, monkeypatch):  # noqa: F811
    """An account as Robert's is on the release before: GeoNames on (its acknowledgment 3 accepted), the
    built-in's list fetched into the control database from a mirror of the tests' files, located clips named
    by its producer (asset_place). The plug-in isn't installed yet."""
    client, robert, tenant_id = install
    mirror = lists.Mirror(lists.write(tmp_path / "site", stored_mb=2))
    env = _env_file(tmp_path, control)
    env.write_text(env.read_text() + f"DATA_DIR={short_data}\n")
    # `lumiverb-server plugins` puts the env file's settings in the environment, where they'd outlast the test:
    # this test's are put there first, to be put back after.
    monkeypatch.setenv("DATA_DIR", str(short_data))
    monkeypatch.setenv("APP_HOST", "http://localhost:6464")
    monkeypatch.setenv("LUMIVERB_GEONAMES_URL", mirror.url)
    get_settings.cache_clear()
    geonames_data.fetch(lambda *a, **k: None)
    _turned_on(tenant_id)
    r = client.post("/v1/libraries", json={"name": "Places", "root_path": "/Volumes/places"}, headers=robert)
    assert r.status_code == 200, r.text
    library_id = r.json()["library_id"]
    clips = {}
    with get_tenant_session(tenant_id) as session:
        for name, (lat, lon) in POINTS.items():
            clips[name] = make_clip(session, library_id, f"{name}.jpg")
            session.execute(text("UPDATE assets SET gps_lat = :lat, gps_lon = :lon WHERE asset_id = :a"),
                            {"lat": lat, "lon": lon, "a": clips[name]})
        clips["picked"] = make_clip(session, library_id, "picked.jpg")
        session.commit()
    for name, (lat, lon) in POINTS.items():
        _named_as_before(tenant_id, clips[name], lat, lon)
    runner = SimpleNamespace(runner=None)
    try:
        yield SimpleNamespace(client=client, robert=robert, tenant_id=tenant_id, library_id=library_id,
                              clips=clips, mirror=mirror, env=env, data=short_data, control=control, runner=runner)
    finally:
        if runner.runner is not None:
            runner.runner.stop_all()
        use_added((), by=INSTALLED)
        mirror.close()


def _detail(f, clip: str) -> dict:
    r = f.client.get(f"/v1/assets/{f.clips[clip]}", headers=f.robert)
    assert r.status_code == 200, r.text
    return r.json()


def _places(f, q: str) -> dict:
    r = f.client.get("/v1/places", params={"q": q}, headers=f.robert)
    assert r.status_code == 200, r.text
    return r.json()


def _still(f) -> int:
    with get_tenant_session(f.tenant_id) as session:
        return still_shown(session)


def _set_by_place(f, place_id: str):
    return f.client.put("/v1/assets/locations", json={"asset_ids": [f.clips["picked"]], "place_id": place_id,
                                                       "replace": "all"}, headers=f.robert)


def test_not_installed_its_old_names_and_list_stand_in(account) -> None:
    f = account
    tower = _detail(f, "tower")
    assert tower["place"]["label"] == "Paris, Île-de-France, France" and tower["place"]["landmark"]["name"] == "Tour Eiffel"
    assert tower["place"]["credit"] == CREDIT and tower["facts"] == []
    assert _still(f) == 4
    # Place search answers from the built-in's list, credited with its license.
    found = _places(f, "paris")
    assert [(p["place_id"], p["label"], p["source"], p["source_title"]) for p in found["places"]] == [
        ("geonames:2988507", "Paris, Île-de-France, France", GEONAMES, "Place names (GeoNames)"),
        ("geonames:4717560", "Paris, Texas, United States", GEONAMES, "Place names (GeoNames)"),
        ("geonames:9999991", "Parisot, Occitanie, France", GEONAMES, "Place names (GeoNames)")]
    assert found["credit"] is None and found["unavailable"] == []
    assert found["credits"] == [{"source": GEONAMES, **CREDIT}]
    # A location set from one of its places keeps the place and its credit.
    r = _set_by_place(f, "geonames:2988507")
    assert r.status_code == 200, r.text
    location = _detail(f, "picked")["location"]
    assert (location["lat"], location["lon"]) == (pytest.approx(lists.PARIS[0]), pytest.approx(lists.PARIS[1]))
    assert location["basis_summary"] == "Paris, Île-de-France, France" and location["credit"] == CREDIT
    with get_tenant_session(f.tenant_id) as session:
        basis = session.execute(text("SELECT basis FROM asset_location WHERE asset_id = :a"),
                                {"a": f.clips["picked"]}).scalar()
    assert basis["place"] == {"id": "geonames:2988507", "label": "Paris, Île-de-France, France", "source": GEONAMES,
                              "credit": CREDIT}
    for bad in ("geonames:1", "geonames:x"):
        assert _set_by_place(f, bad).status_code == 422
    # The old producer's routes are gone, naming the plug-in's work.
    for route in ("/v1/places/nearest", "/v1/places/names"):
        r = f.client.post(route, json={}, headers=f.robert)
        assert r.status_code == 410 and r.json()["error"]["code"] == "gone", r.text
        assert r.json()["error"]["details"]["replaced_by"][0].startswith("POST /v1/plugins/geonames/")
    # Processing works without it: its row gone, failures and retry answering, the old producer unknown.
    r = f.client.get("/v1/producers", headers=f.robert)
    assert r.status_code == 200 and "place" not in {p["artifact"] for p in r.json()["producers"]}, r.text
    assert f.client.get("/v1/producers/failures", headers=f.robert).status_code == 200
    assert f.client.post("/v1/producers/failures/retry", json={"all": True}, headers=f.robert).status_code == 200
    r = f.client.post("/v1/producers/failures/retry", json={"all": True, "artifact": "place"}, headers=f.robert)
    assert r.status_code == 404, r.text
    for scope in ("new", "redo"):  # Run and Remake
        r = f.client.post("/v1/producers/run", json={"producer": "all", "all": True, "scope": scope}, headers=f.robert)
        assert r.status_code == 200 and "place" not in {p["artifact"] for p in r.json()["producers"]}, r.text
    # Turned off in the account: its old names hidden, its search off, a place not set.
    _turned_on(f.tenant_id, on=False)
    assert _detail(f, "tower")["place"] is None and _still(f) == 0
    r = f.client.get("/v1/places", params={"q": "paris"}, headers=f.robert)
    assert r.status_code == 409 and r.json()["error"]["code"] == "plugin_off", r.text
    r = _set_by_place(f, "geonames:2988507")
    assert r.status_code == 409 and r.json()["error"]["code"] == "plugin_off", r.text
    assert _detail(f, "picked")["location"]["credit"] == CREDIT, "what was set stays credited"


def _install_and_run(f):
    install_own(get_settings().control_plane_database_url, f.data, geonames_url=get_settings().lumiverb_geonames_url,
                say=lambda line: None)
    refresh()
    from src.server.plugin_runner import Runner

    f.runner.runner = Runner(f.data)
    return f.runner.runner


def _running(f) -> dict:
    return _listed(f.client, f.robert)[GEONAMES]["running"]


def test_installed_it_takes_over_once_its_list_is_there_the_old_names_meanwhile(account) -> None:
    f = account
    f.mirror.hold("allCountries.zip", lists.MB)
    runner = _install_and_run(f)
    listed = _listed(f.client, f.robert)[GEONAMES]
    assert (listed["kind"], listed["state"], listed["version"]) == ("installed", "on", "1"), "on as it was, unasked"
    _until(runner, lambda: _running(f)["state"] == "preparing")
    # While it fetches its first list: today's list answers, nothing said unavailable, the old names shown.
    found = _places(f, "eiffel")
    assert [(p["name"], p["matched"]) for p in found["places"]] == [("Tour Eiffel", "Eiffel Tower"), ("Eiffel Peak", None)]
    assert found["unavailable"] == [] and found["credits"] == [{"source": GEONAMES, **CREDIT}]
    assert _set_by_place(f, "geonames:6254976").status_code == 200
    assert _detail(f, "town")["place"]["label"] == "Paris, Île-de-France, France"
    before = {q: _places(f, q)["places"] for q in ("paris", "zur", "magic", "sampa", "tied")}

    f.mirror.release()
    _until(runner, lambda: _running(f)["state"] == "ready" and not _running(f).get("detail"))
    # The plug-in answers now: the same places, the same ids.
    for q, places in before.items():
        assert _places(f, q)["places"] == places, q
    with get_control_session() as session:  # the built-in's list isn't asked any more
        session.execute(text("DELETE FROM geonames_places"))
        session.execute(text("DELETE FROM geonames_landmarks"))
        session.commit()
    assert [p["name"] for p in _places(f, "paris")["places"]] == ["Paris", "Paris", "Parisot"]
    assert _set_by_place(f, "geonames:2988507").status_code == 200

    # Its work names every located clip; then the detail shows its facts and no old name.
    acct = FakeAccount(scheduler_client(f.tenant_id))
    acct.tenant_id = f.tenant_id
    items = due(tenant_database_url(f.tenant_id), "plugin_geonames", [f.library_id])
    assert {i["asset_id"] for i in items} == {f.clips[n] for n in (*POINTS, "picked")}
    run_job(PluginWork, acct, f.tenant_id, "plugin_geonames", items)
    assert acct.failures.charged == []
    said = {}
    for name in POINTS:
        detail = _detail(f, name)
        assert detail["place"] is None, name
        section = next(s for s in detail["facts"] if s["plugin_id"] == GEONAMES)
        assert section["credit"] == CREDIT
        said[name] = {fact["key"]: (fact["text"], fact["empty"]) for fact in section["facts"]}
    assert said["tower"] == {"at": ("Tour Eiffel", False), "near": ("Paris, Île-de-France, France", False)}
    assert said["park"] == {"at": ("Magic Kingdom, Walt Disney World, Florida, United States", False),
                            "near": ("Not near a town", True)}
    assert said["town"] == {"near": ("Paris, Île-de-France, France", False)}
    assert said["sea"] == {"near": ("Not near a town", True)}
    assert _still(f) == 0
    # The count the removal waits on, as `lumiverb-server plugins` says it.
    import io
    from contextlib import redirect_stdout

    out = io.StringIO()
    with redirect_stdout(out):
        assert cli.main(["plugins", "--env", str(f.env)]) == 0
    assert "0 clips still showing an old place name" in out.getvalue()

    # Uninstalled: a location set from one of its places keeps its credit.
    assert cli.main(["plugins", "uninstall", GEONAMES, "--yes", "--env", str(f.env)]) == 0
    location = _detail(f, "picked")["location"]
    assert location["basis_summary"] == "Paris, Île-de-France, France" and location["credit"] == CREDIT


def test_on_in_no_account_it_fetches_nothing_until_one_turns_it_on(account) -> None:
    f = account
    _turned_on(f.tenant_id, on=False)
    before = list(f.mirror.asked)  # the built-in's own fetch, the release before
    runner = _install_and_run(f)
    _until(runner, lambda: _running(f)["state"] == "off")
    for _ in range(10):
        runner.turn()
    assert f.mirror.asked == before, "nothing fetched while no account has it on"
    r = f.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": True, "acknowledged": "3"}, headers=f.robert)
    assert r.status_code == 200, r.text
    _until(runner, lambda: _running(f)["state"] == "ready" and not _running(f).get("detail"))
    assert f.mirror.asked[:len(before)] == before and f.mirror.gets("cities1000.zip") == 2
