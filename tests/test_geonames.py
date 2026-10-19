"""The Place names plug-in (ADR-021 phase 2) against the real API and
database, with a local web server standing in for download.geonames.org:
nothing fetched until an account turns it on, the place list fetched once
for the server, its progress and failures said, upkeep taking up a fetch
that stopped, search by name (the exact name first, then the most
populous, narrowed by region or country), landmarks beside the towns (the
kinds kept, with a Wikipedia article or every one), places found by their
names in other languages, a list from an older release fetched again while
the old one is used, a location set by place, and the GeoNames credit
beside every name."""

from __future__ import annotations

import http.server
import io
import threading
import time
import zipfile
from datetime import timedelta
from functools import partial

import pytest
from sqlalchemy import text

from src.plugins import registry
from src.plugins.geonames import data as geonames_data
from src.plugins.geonames.data import (
    parse_countries,
    parse_landmarks,
    parse_names,
    parse_places,
    parse_regions,
    wikipedia_linked,
)
from src.server.config import get_settings
from src.server.database import get_control_session
from src.server.repository.places import fold
from src.shared.utils import utcnow
from tests.test_scans_api import File, _library, _scan, env  # noqa: F401 — the shared fixture

pytestmark = pytest.mark.slow
GEONAMES = "geonames"


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


@pytest.fixture(scope="module")
def geonames_site(tmp_path_factory):
    """A local stand-in for download.geonames.org: its three files; the URL it serves them at."""
    root = tmp_path_factory.mktemp("geonames")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cities1000.txt", "\n".join(PLACES) + "\n")
    (root / "cities1000.zip").write_bytes(buf.getvalue())
    (root / "admin1CodesASCII.txt").write_text(REGIONS, encoding="utf-8")
    (root / "countryInfo.txt").write_text(COUNTRIES, encoding="utf-8")
    for name, member, lines in (("allCountries.zip", "allCountries.txt", "\n".join(FEATURES) + "\n"),
                                ("alternateNamesV2.zip", "alternateNamesV2.txt", ALTERNATE_NAMES)):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(member, lines)
        (root / name).write_bytes(buf.getvalue())
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None  # type: ignore[attr-defined]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


@pytest.fixture(autouse=True)
def _fresh(env, geonames_site, monkeypatch, tmp_path):  # noqa: F811
    """Every test: the plug-in off in both accounts, nothing fetched, the stand-in site, and a data
    folder of its own (what's downloaded goes there: never another worker's at the same time)."""
    monkeypatch.setattr(get_settings(), "lumiverb_geonames_url", geonames_site)
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    _forget()
    yield
    for headers in (env.admin, env.other):
        env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": False}, headers=headers)
    _wait_quiet()
    _forget()


def _forget() -> None:
    with get_control_session() as session:
        for table in ("plugin_data", "geonames_places", "geonames_landmarks", "geonames_admin1", "geonames_countries"):
            session.execute(text(f"DELETE FROM {table}"))
        session.commit()


def _data(env, headers=None) -> dict:  # noqa: F811
    r = env.client.get("/v1/plugins", headers=headers or env.admin)
    assert r.status_code == 200, r.text
    return next(p for p in r.json()["plugins"] if p["plugin_id"] == GEONAMES)["data"]


def _wait_quiet() -> None:
    """Until no fetch is running (a test's background thread)."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        with get_control_session() as session:
            if not session.execute(text("SELECT 1 FROM plugin_data WHERE state = 'fetching'")).first():
                return
        time.sleep(0.05)


def _turn_on(env, headers=None) -> dict:  # noqa: F811
    version = registry()[GEONAMES].acknowledgment.version
    r = env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": True, "acknowledged": version},
                       headers=headers or env.admin)
    assert r.status_code == 200, r.text
    return r.json()


def _until(env, state: str, headers=None) -> dict:  # noqa: F811
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        data = _data(env, headers)
        if data["state"] == state:
            return data
        time.sleep(0.05)
    raise AssertionError(f"never {state}: {data}")


def _search(env, q: str, headers=None, **params):  # noqa: F811
    return env.client.get("/v1/places", params={"q": q, **params}, headers=headers or env.admin)


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


def test_nothing_is_fetched_or_found_until_an_account_turns_it_on(env) -> None:  # noqa: F811
    assert _data(env) == {"title": "The place list", "unit": "places", "state": "missing", "done": 0,
                          "total": None, "detail": None, "error": None, "count": None, "ready_at": None}
    r = _search(env, "Paris")
    assert r.status_code == 409 and r.json()["error"]["code"] == "plugin_off", r.text
    assert r.json()["error"]["details"]["plugin_id"] == GEONAMES
    with get_control_session() as session:
        assert session.execute(text("SELECT count(*) FROM geonames_places")).scalar() == 0

    on = _turn_on(env)
    assert on["data"]["state"] in ("fetching", "ready")
    ready = _until(env, "ready")
    assert ready["count"] == COUNT and ready["ready_at"] and ready["error"] is None
    assert not geonames_data.folder().exists()  # the downloads, deleted once read
    # Another account turning it on uses what's there.
    assert _search(env, "Paris", env.other).status_code == 409  # off there
    _turn_on(env, env.other)
    assert _data(env, env.other)["state"] == "ready" and _search(env, "Paris", env.other).status_code == 200


def test_places_are_found_by_name_most_populous_first_and_narrowed(env) -> None:  # noqa: F811
    _turn_on(env)
    _until(env, "ready")
    r = _search(env, "paris")
    assert r.status_code == 200, r.text
    found = r.json()["places"]
    assert [p["label"] for p in found] == ["Paris, Île-de-France, France", "Paris, Texas, United States",
                                           "Parisot, Occitanie, France"]
    assert found[0]["place_id"] == "geonames:2988507" and found[0]["lat"] == pytest.approx(48.85341)
    assert r.json()["credit"]["license_url"] == "https://creativecommons.org/licenses/by/4.0/"
    assert "GeoNames" in r.json()["credit"]["text"]
    assert [p["label"] for p in _search(env, "Paris, tex").json()["places"]] == ["Paris, Texas, United States"]
    assert [p["label"] for p in _search(env, "paris, us").json()["places"]] == ["Paris, Texas, United States"]
    # Accents or not, and GeoNames' own ASCII spelling.
    for typed in ("zurich", "Zürich", "ZUR", "zuerich"):
        assert [p["name"] for p in _search(env, typed).json()["places"]] == ["Zürich"], typed
    assert [p["label"] for p in _search(env, "sao paulo").json()["places"]] == ["São Paulo, Brazil"]
    assert [p["label"] for p in _search(env, "paris, ile").json()["places"]] == ["Paris, Île-de-France, France"]
    assert [p["label"] for p in _search(env, "Port of").json()["places"]] == ["Port of Spain, Trinidad and Tobago"]
    assert _search(env, "%").json()["places"] == [] and _search(env, "par", limit=1).json()["places"][0]["name"] == "Paris"
    assert env.client.get("/v1/places", params={"q": "Paris"}).status_code == 401  # signed in only
    assert _search(env, "Paris", env.viewer).status_code == 200


def test_a_location_set_by_place_keeps_the_place_and_its_credit(env) -> None:  # noqa: F811
    lib = _library(env)
    clip = _scan(env, lib, [File("trip.jpg", f"trip {lib}")], headers=env.admin).uploaded["trip.jpg"]
    body = {"asset_ids": [clip], "place_id": "geonames:3573890"}
    r = env.client.put("/v1/assets/locations", json=body, headers=env.editor)
    assert r.status_code == 409 and r.json()["error"]["code"] == "plugin_off", r.text

    _turn_on(env)
    _until(env, "ready")
    for bad in ("geonames:1", "nope", "geonames:x"):
        r = env.client.put("/v1/assets/locations", json={**body, "place_id": bad}, headers=env.editor)
        assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_place", r.text
    r = env.client.put("/v1/assets/locations", json={**body, "lat": 1.0, "lon": 1.0}, headers=env.editor)
    assert r.status_code == 400  # one of lat and lon, same_as, place_id
    r = env.client.put("/v1/assets/locations", json=body, headers=env.editor)
    assert r.status_code == 200 and r.json()["updated"] == [clip], r.text
    location = env.client.get(f"/v1/assets/{clip}", headers=env.admin).json()["location"]
    assert (location["lat"], location["lon"]) == (pytest.approx(10.66668), pytest.approx(-61.51889))
    assert location["source"] == "person" and location["basis_summary"] == "Port of Spain, Trinidad and Tobago"
    assert location["credit"] == {"text": "GeoNames", "url": "https://www.geonames.org/",
                                  "license": {"text": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"}}
    public = env.client.get(f"/v1/assets/{clip}", params={"public_library_id": lib})
    assert "Port of Spain" not in public.text


def test_a_failed_fetch_says_why_and_is_tried_again(env, monkeypatch, geonames_site) -> None:  # noqa: F811
    monkeypatch.setattr(get_settings(), "lumiverb_geonames_url", geonames_site + "missing/")
    _turn_on(env)
    failed = _until(env, "failed")
    assert "404" in failed["error"]
    r = _search(env, "Paris")
    assert r.status_code == 503 and r.json()["error"]["code"] == "places_not_ready"
    assert r.json()["error"]["details"]["state"] == "failed"

    # Upkeep waits an hour before trying a failed fetch again; turning it on again (Try again) doesn't.
    monkeypatch.setattr(get_settings(), "lumiverb_geonames_url", geonames_site)
    r = env.client.post("/v1/upkeep", headers=env.admin)
    assert r.status_code == 200 and r.json()["plugin_data"] == {GEONAMES: "waiting"}, r.text
    _turn_on(env)
    assert _until(env, "ready")["count"] == COUNT


def test_upkeep_takes_up_a_fetch_that_stopped(env) -> None:  # noqa: F811
    _turn_on(env)
    _until(env, "ready")
    _forget()
    with get_control_session() as session:  # a fetch the API's restart cut off, long ago
        session.execute(text("INSERT INTO plugin_data (plugin_id, state, done, updated_at)"
                             " VALUES (:p, 'fetching', 10, :at)"), {"p": GEONAMES, "at": utcnow() - timedelta(hours=1)})
        session.commit()
    r = env.client.post("/v1/upkeep", headers=env.admin)
    assert r.status_code == 200 and r.json()["plugin_data"] == {GEONAMES: "started"}, r.text
    assert _until(env, "ready")["count"] == COUNT
    r = env.client.post("/v1/upkeep", headers=env.admin)
    assert r.json()["plugin_data"] == {GEONAMES: "ready"}
    # Off in every account: upkeep leaves it be.
    env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": False}, headers=env.admin)
    assert env.client.post("/v1/upkeep", headers=env.admin).json()["plugin_data"] == {}


def test_landmarks_are_found_by_name_after_the_towns(env) -> None:  # noqa: F811
    _turn_on(env)
    _until(env, "ready")
    found = _search(env, "magic").json()["places"]
    assert [(p["label"], p["kind"], p["population"]) for p in found] == [
        ("Magic Kingdom, Walt Disney World, Florida, United States", "amusement park", 0)]
    assert found[0]["place_id"] == "geonames:4164161"
    assert [p["name"] for p in _search(env, "tour eif").json()["places"]] == ["Tour Eiffel"]
    assert [p["name"] for p in _search(env, "champ, france").json()["places"]] == ["Champ de Mars"]
    assert _search(env, "paris").json()["places"][0]["kind"] is None  # a town
    for left_out in ("Hôtel du Coin", "Ruisseau"):
        assert _search(env, left_out).json()["places"] == [], left_out
    # A location set by a landmark's name.
    lib = _library(env)
    clip = _scan(env, lib, [File("castle.jpg", f"castle {lib}")], headers=env.admin).uploaded["castle.jpg"]
    r = env.client.put("/v1/assets/locations", json={"asset_ids": [clip], "place_id": "geonames:4164161"},
                       headers=env.editor)
    assert r.status_code == 200, r.text
    location = env.client.get(f"/v1/assets/{clip}", headers=env.admin).json()["location"]
    assert (location["lat"], location["lon"]) == (pytest.approx(MAGIC_KINGDOM[0]), pytest.approx(MAGIC_KINGDOM[1]))
    assert location["basis_summary"] == "Magic Kingdom, Walt Disney World, Florida, United States"


def test_a_list_from_an_older_release_is_fetched_again_while_it_is_used(env) -> None:  # noqa: F811
    from src.server import plugin_data

    _turn_on(env)
    _until(env, "ready")
    assert plugin_data.status(GEONAMES)["current"] is True
    # The list a release before landmarks fetched: towns only, its version unsaid (1).
    with get_control_session() as session:
        session.execute(text("DELETE FROM geonames_landmarks"))
        session.execute(text("UPDATE plugin_data SET version = NULL, count = 6"))
        session.commit()
    assert plugin_data.status(GEONAMES)["current"] is False
    assert _search(env, "paris").status_code == 200  # what's there is used meanwhile
    # The producer's lookups want this release's: they wait, and it's fetched again.
    lib = _library(env)
    clip = _scan(env, lib, [File("there.jpg", f"there {lib}")], headers=env.admin).uploaded["there.jpg"]
    r = env.client.post("/v1/places/nearest", json={"asset_ids": [clip], "within_km": 25}, headers=env.admin)
    assert r.status_code == 503 and r.json()["error"]["code"] == "places_not_ready", r.text
    deadline = time.monotonic() + 60
    while not plugin_data.status(GEONAMES)["current"] and time.monotonic() < deadline:
        time.sleep(0.05)
    data = _until(env, "ready")
    assert data["count"] == COUNT
    assert plugin_data.status(GEONAMES)["version"] == registry()[GEONAMES].data_version
    assert [p["name"] for p in _search(env, "magic").json()["places"]] == ["Magic Kingdom, Walt Disney World"]
    r = env.client.post("/v1/places/nearest", json={"asset_ids": [clip], "within_km": 25}, headers=env.admin)
    assert r.status_code == 200, r.text
    # Upkeep leaves this release's list be.
    assert env.client.post("/v1/upkeep", headers=env.admin).json()["plugin_data"] == {GEONAMES: "ready"}


def test_places_are_found_by_their_names_in_other_languages(env) -> None:  # noqa: F811
    _turn_on(env)
    _until(env, "ready")
    found = _search(env, "zurigo").json()["places"]
    assert [(p["name"], p["matched"]) for p in found] == [("Zürich", "Zurigo")]
    # The same name in French and English, French listed first: kept once, as English.
    with get_control_session() as session:
        assert session.execute(text("SELECT language FROM geonames_names WHERE geonameid = 6254976"
                                    " AND folded = 'eiffel tower'")).scalars().all() == ["en"]
    # Several of its names start so: the English one says which. Landmarks by how known they are (their
    # other names): the tower, then the peak (found by its own name).
    assert [(p["name"], p["matched"]) for p in _search(env, "eiffel").json()["places"]] == [
        ("Tour Eiffel", "Eiffel Tower"), ("Eiffel Peak", None)]
    assert [(p["name"], p["matched"]) for p in _search(env, "eiffelt").json()["places"]] == [
        ("Tour Eiffel", "Eiffelturm")]
    # Found by its own name: said so, once.
    assert [(p["name"], p["matched"]) for p in _search(env, "zur").json()["places"]] == [("Zürich", None)]
    # Exactly a place's own name before exactly another's (with no language), though that's more populous.
    assert [(p["name"], p["matched"]) for p in _search(env, "sampa").json()["places"]] == [
        ("Sampa", None), ("São Paulo", "Sampa")]
    # Exactly another's before starting with its own: "zurigo" is Zürich's whole name in Italian.
    assert [p["name"] for p in _search(env, "zurig").json()["places"]] == ["Zürich"]
    for left_out in ("turicum", "zurchy", "8001", "le ruisseau"):
        assert _search(env, left_out).json()["places"] == [], left_out
    # Narrowed by where, as by its own name.
    assert _search(env, "zurigo, brazil").json()["places"] == []
    assert [p["name"] for p in _search(env, "zurigo, switz").json()["places"]] == ["Zürich"]
