"""The Place names plug-in (ADR-021 phase 2) against the real API and
database, with a local web server standing in for download.geonames.org:
nothing fetched until an account turns it on, the place list fetched once
for the server, its progress and failures said, upkeep taking up a fetch
that stopped, search by name (the exact name first, then the most
populous, narrowed by region or country), a location set by place, and the
GeoNames credit beside every name."""

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
from src.plugins.geonames.data import parse_countries, parse_places, parse_regions
from src.server.config import get_settings
from src.server.database import get_control_session
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
    "not a place line",
]
REGIONS = "TT.05\tPort of Spain\tPort of Spain\t3573891\nFR.11\tÎle-de-France\tIle-de-France\t3012874\n" \
          "US.TX\tTexas\tTexas\t4736286\nCH.ZH\tZurich\tZurich\t2657895\nFR.76\tOccitanie\tOccitanie\t11071623\n" \
          "BR.27\tSão Paulo\tSao Paulo\t3448433\n"
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
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None  # type: ignore[attr-defined]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


@pytest.fixture(autouse=True)
def _fresh(env, geonames_site, monkeypatch):  # noqa: F811
    """Every test: the plug-in off in both accounts, nothing fetched, the stand-in site."""
    monkeypatch.setattr(get_settings(), "lumiverb_geonames_url", geonames_site)
    _forget()
    yield
    for headers in (env.admin, env.other):
        env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": False}, headers=headers)
    _wait_quiet()
    _forget()


def _forget() -> None:
    with get_control_session() as session:
        for table in ("plugin_data", "geonames_places", "geonames_admin1", "geonames_countries"):
            session.execute(text(f"DELETE FROM {table}"))
        session.commit()


def _data(env, headers=None) -> dict:  # noqa: F811
    r = env.client.get("/v1/plugins", headers=headers or env.admin)
    assert r.status_code == 200, r.text
    return next(p for p in r.json()["plugins"] if p["plugin_id"] == GEONAMES)["data"]


def _wait_quiet() -> None:
    """Until no fetch is running (a test's background thread)."""
    for _ in range(200):
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
    for _ in range(300):
        data = _data(env, headers)
        if data["state"] == state:
            return data
        time.sleep(0.05)
    raise AssertionError(f"never {state}: {data}")


def _search(env, q: str, headers=None, **params):  # noqa: F811
    return env.client.get("/v1/places", params={"q": q, **params}, headers=headers or env.admin)


def test_the_files_are_read_as_geonames_writes_them() -> None:
    places = list(parse_places(iter(line + "\n" for line in PLACES)))
    assert [p["geonameid"] for p in places] == [3573890, 2988507, 4717560, 2657896, 3448439, 9999991]
    zurich = places[3]
    assert (zurich["name"], zurich["ascii_name"], zurich["admin1_code"], zurich["population"]) == (
        "Zürich", "Zuerich", "ZH", 415367)
    assert {r["code"]: r["name"] for r in parse_regions(REGIONS)}["FR.11"] == "Île-de-France"
    assert {c["iso"]: c["name"] for c in parse_countries(COUNTRIES)} == {
        "TT": "Trinidad and Tobago", "FR": "France", "US": "United States", "CH": "Switzerland", "BR": "Brazil"}


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
    assert ready["count"] == 6 and ready["ready_at"] and ready["error"] is None
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
    assert location["credit"] == {"text": "GeoNames", "url": "https://www.geonames.org/"}
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
    assert _until(env, "ready")["count"] == 6


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
    assert _until(env, "ready")["count"] == 6
    r = env.client.post("/v1/upkeep", headers=env.admin)
    assert r.json()["plugin_data"] == {GEONAMES: "ready"}
    # Off in every account: upkeep leaves it be.
    env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": False}, headers=env.admin)
    assert env.client.post("/v1/upkeep", headers=env.admin).json()["plugin_data"] == {}
