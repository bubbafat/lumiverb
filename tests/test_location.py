# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Location, ADR-017 phases 1 and 2.

Phase 1: the scan rejects (0, 0) and keeps the time offset and GPS
accuracy; the effective location (a person's, then the file's, then an
applied guess only with include_guesses; a suggestion never) drives
has_gps, near and the location facet. Phase 2: a person sets, clears or
accepts a location for clips named by id; replacing one needs the
request's say (409 location_exists).
"""

from __future__ import annotations

import io
import json
import os

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session

from src.client.workers.exif_extract import parse_gps, parse_gps_accuracy_m, parse_taken_at_offset_min
from src.server.models.filter_registry import parse_f_params
from src.server.models.query_filter import LOCATION_FILTERS, IncludeGuesses
from src.shared.location import is_fix
from tests.machine_lineage import ingest_made
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture

# ---------------------------------------------------------------------------
# Fast: EXIF and the filter
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_zero_zero_is_no_fix() -> None:
    assert parse_gps({"GPSLatitude": 0.0, "GPSLongitude": 0.0}) == (None, None)
    assert parse_gps({"GPSLatitude": 0.0, "GPSLongitude": 12.5}) == (0.0, 12.5)
    assert parse_gps({"GPSLatitude": 48.85, "GPSLatitudeRef": "S", "GPSLongitude": 2.29,
                      "GPSLongitudeRef": "W"}) == (-48.85, -2.29)
    assert not is_fix(None, 1) and not is_fix(91, 0) and not is_fix(float("nan"), 1) and is_fix(1, 1)


@pytest.mark.fast
@pytest.mark.parametrize("exif,want", [
    ({"OffsetTimeOriginal": "+02:00"}, 120),
    ({"OffsetTimeOriginal": "-05:30"}, -330),
    ({"OffsetTime": "+0545"}, 345),
    ({"OffsetTimeOriginal": "Z"}, 0),
    ({"OffsetTimeOriginal": "junk", "OffsetTime": "+01:00"}, 60),
    ({"OffsetTimeOriginal": "+15:00"}, None),
    ({}, None),
])
def test_time_offset_in_minutes(exif, want) -> None:
    assert parse_taken_at_offset_min(exif) == want


@pytest.mark.fast
def test_gps_accuracy() -> None:
    assert parse_gps_accuracy_m({"GPSHPositioningError": 4.7}) == 4.7
    assert parse_gps_accuracy_m({"GPSHPositioningError": "12 m"}) == 12.0
    assert parse_gps_accuracy_m({"GPSHPositioningError": -1}) is None
    assert parse_gps_accuracy_m({}) is None


@pytest.mark.fast
def test_include_guesses_is_a_location_filter() -> None:
    spec = parse_f_params(["has_gps:yes", "include_guesses:yes"])
    assert any(isinstance(leaf, IncludeGuesses) for leaf in spec.leaves)
    assert IncludeGuesses in LOCATION_FILTERS
    assert spec.needs_location_join
    params: dict = {}
    for leaf in spec.leaves:
        leaf.to_sql(params, [0])
    assert params["include_guesses"] is True
    params = {}
    parse_f_params(["has_gps:yes"]).leaves[0].to_sql(params, [0])
    assert params["include_guesses"] is False


# ---------------------------------------------------------------------------
# Slow: against the API
# ---------------------------------------------------------------------------


def _db(env):
    return Session(create_engine(env[-1]))


def _ingest(env, rel_path: str, exif: dict | None = None) -> str:
    client, headers, library_id, *_ = env
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 18)).save(buf, format="JPEG")
    buf.seek(0)
    data = {"library_id": library_id, "rel_path": rel_path, "file_size": "10", "media_type": "image",
            "width": "32", "height": "18", "lineage": ingest_made(None),
            "exif": json.dumps({"sha256": os.urandom(32).hex(), **(exif or {})})}
    r = client.post("/v1/ingest", data=data, files={"proxy": ("p.jpg", buf, "image/jpeg")}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["asset_id"]


def _put(env, **body):
    client, headers, *_ = env
    return client.put("/v1/assets/locations", json=body, headers=headers)


def _query(env, *filters: str) -> set[str]:
    client, headers, library_id, *_ = env
    r = client.get("/v1/query", params=[("f", f"library:{library_id}"), *(("f", f) for f in filters)],
                   headers=headers)
    assert r.status_code == 200, r.text
    return {i["asset_id"] for i in r.json()["items"]}


def _facets(env, *filters: str) -> dict:
    client, headers, library_id, *_ = env
    r = client.get("/v1/assets/facets", params=[("f", f"library:{library_id}"), *(("f", f) for f in filters)],
                   headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _detail(env, asset_id: str) -> dict:
    client, headers, *_ = env
    r = client.get(f"/v1/assets/{asset_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.slow
def test_the_scan_keeps_offset_and_accuracy_and_drops_zero_zero(env):
    good = _ingest(env, "exif/good.jpg", {"gps_lat": 10.5, "gps_lon": 20.5, "gps_accuracy_m": 4.5,
                                          "taken_at_offset_min": 120})
    zero = _ingest(env, "exif/zero.jpg", {"gps_lat": 0, "gps_lon": 0, "gps_accuracy_m": 3.0,
                                          "taken_at_offset_min": 99999})
    with _db(env) as s:
        rows = dict((r[0], r[1:]) for r in s.execute(text(
            "SELECT asset_id, gps_lat, gps_lon, gps_accuracy_m, taken_at_offset_min FROM assets"
            " WHERE asset_id IN (:g, :z)"), {"g": good, "z": zero}).all())
    assert rows[good] == (10.5, 20.5, 4.5, 120)
    assert rows[zero] == (None, None, None, None)
    assert _detail(env, good)["gps_accuracy_m"] == 4.5


@pytest.mark.slow
@pytest.mark.parametrize("body", [None, {}, {"asset_ids": []}, {"lat": 1, "lon": 2}])
def test_every_location_write_names_its_clips(env, body):
    client, headers, *_ = env
    for method, url in (("PUT", "/v1/assets/locations"), ("DELETE", "/v1/assets/locations"),
                        ("POST", "/v1/assets/locations/accept")):
        kwargs = {} if body is None else {"json": body}
        r = client.request(method, url, headers=headers, **kwargs)
        assert r.status_code == 400, (method, body, r.status_code, r.text)


@pytest.mark.slow
def test_a_persons_location_counts_and_is_recorded_as_theirs(env):
    bare = _ingest(env, "person/bare.jpg")
    assert bare not in _query(env, "has_gps:yes")
    assert bare in _query(env, "has_gps:no")

    r = _put(env, asset_ids=[bare, "ast_nope"], lat=51.5, lon=-0.12)
    assert r.status_code == 200, r.text
    assert r.json() == {"updated": [bare], "skipped": ["ast_nope"]}

    assert bare in _query(env, "has_gps:yes")
    assert bare not in _query(env, "has_gps:no")
    assert bare in _query(env, "near:51.5,-0.12,1")
    assert bare not in _query(env, "near:40,-74,1")
    page = env[0].get("/v1/assets/page", params={"library_id": env[2], "has_gps": "true"}, headers=env[1]).json()
    assert bare in {i["asset_id"] for i in page["items"]}

    d = _detail(env, bare)
    assert d["gps_lat"] is None  # the file says nothing; it stays so
    assert d["location"]["lat"] == 51.5 and d["location"]["source"] == "person"
    assert d["location"]["radius_m"] == 0 and d["location"]["status"] == "applied"
    with _db(env) as s:
        assert s.execute(text("SELECT producer FROM artifact_lineage WHERE asset_id = :a AND artifact = 'location'"),
                         {"a": bare}).scalar() == "person"

    r = env[0].request("DELETE", "/v1/assets/locations", json={"asset_ids": [bare]}, headers=env[1])
    assert r.status_code == 200 and r.json() == {"cleared": [bare], "skipped": []}
    assert bare not in _query(env, "has_gps:yes")
    assert _detail(env, bare)["location"] is None
    with _db(env) as s:
        assert s.execute(text("SELECT count(*) FROM artifact_lineage WHERE asset_id = :a AND artifact = 'location'"),
                         {"a": bare}).scalar() == 0


@pytest.mark.slow
def test_replacing_a_location_needs_the_requests_say(env):
    filed = _ingest(env, "replace/file.jpg", {"gps_lat": 40.0, "gps_lon": -74.0})
    bare = _ingest(env, "replace/bare.jpg")

    r = _put(env, asset_ids=[filed, bare], lat=1.0, lon=2.0)
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "location_exists"
    assert err["details"] == {"count": 1, "person": 0, "file": 1, "total": 2}
    assert _detail(env, bare)["location"] is None  # nothing changed

    assert _put(env, asset_ids=[filed], lat=1.0, lon=2.0, replace="person").status_code == 409
    assert _put(env, asset_ids=[filed, bare], lat=1.0, lon=2.0, replace="all").status_code == 200
    d = _detail(env, filed)
    assert (d["gps_lat"], d["gps_lon"]) == (40.0, -74.0)  # the file's stays visible
    assert d["location"]["lat"] == 1.0
    # The person's wins in filters.
    assert filed in _query(env, "near:1,2,1") and filed not in _query(env, "near:40,-74,1")

    r = _put(env, asset_ids=[bare], lat=3.0, lon=4.0)
    assert r.status_code == 409 and r.json()["error"]["details"]["person"] == 1
    assert _put(env, asset_ids=[bare], lat=3.0, lon=4.0, replace="person").status_code == 200
    assert _detail(env, bare)["location"]["lat"] == 3.0


@pytest.mark.slow
def test_same_place_as_another_clip(env):
    source = _ingest(env, "same/source.jpg", {"gps_lat": 35.0, "gps_lon": 139.0})
    target = _ingest(env, "same/target.jpg")
    nowhere = _ingest(env, "same/nowhere.jpg")

    r = _put(env, asset_ids=[source, target], same_as=source)
    assert r.status_code == 200, r.text
    assert r.json() == {"updated": [target], "skipped": [source]}
    loc = _detail(env, target)["location"]
    assert (loc["lat"], loc["lon"]) == (35.0, 139.0)
    assert loc["basis_summary"] == "Same place as source.jpg"

    assert _put(env, asset_ids=[target], same_as=nowhere).status_code == 400
    assert _put(env, asset_ids=[target], same_as=source, lat=1, lon=1).status_code == 400
    assert _put(env, asset_ids=[target], lat=0, lon=0, replace="all").status_code == 400


def _row(env, asset_id: str, source: str, status: str, lat: float, lon: float) -> None:
    with _db(env) as s:
        s.execute(text(
            "INSERT INTO asset_location (asset_id, lat, lon, radius_m, source, status, basis, set_at)"
            " VALUES (:a, :lat, :lon, 2000, :src, :st, '{}', now())"),
            {"a": asset_id, "lat": lat, "lon": lon, "src": source, "st": status})
        s.commit()


@pytest.mark.slow
def test_guesses_count_only_when_asked_and_suggestions_never(env):
    guessed = _ingest(env, "guess/guessed.jpg")
    suggested = _ingest(env, "guess/suggested.jpg")
    _row(env, guessed, "time", "applied", -33.9, 151.2)
    _row(env, suggested, "suggestion", "suggested", -33.9, 151.2)

    assert guessed not in _query(env, "has_gps:yes")
    assert guessed in _query(env, "has_gps:yes", "include_guesses:yes")
    assert guessed not in _query(env, "near:-33.9,151.2,1")
    assert guessed in _query(env, "near:-33.9,151.2,1", "include_guesses:yes")
    for filters in (("has_gps:yes",), ("has_gps:yes", "include_guesses:yes"), ("near:-33.9,151.2,1", "include_guesses:yes")):
        assert suggested not in _query(env, *filters), filters

    without, with_ = _facets(env), _facets(env, "include_guesses:yes")
    assert with_["has_gps_count"] == without["has_gps_count"] + 1
    assert without["guess_count"] >= 1

    # A guess is replaced without asking (it's derived); a person's then wins.
    assert _put(env, asset_ids=[guessed], lat=5.0, lon=6.0).status_code == 200
    assert _detail(env, guessed)["location"]["source"] == "person"

    # include_guesses alone is a modifier: it matches everything, never fails.
    assert guessed in _query(env, "include_guesses:yes")
    assert "guess_count" in _facets(env, "include_guesses:yes")

    # Accepting a suggestion makes it a person's.
    r = env[0].post("/v1/assets/locations/accept", json={"asset_ids": [suggested, guessed]}, headers=env[1])
    assert r.status_code == 200 and r.json() == {"accepted": [suggested], "skipped": [guessed]}
    loc = _detail(env, suggested)["location"]
    assert (loc["source"], loc["status"], loc["radius_m"]) == ("person", "applied", 2000)
    assert suggested in _query(env, "has_gps:yes")


@pytest.mark.slow
def test_accepting_a_suggestion_over_the_files_gps_asks(env):
    filed = _ingest(env, "accept/filed.jpg", {"gps_lat": 12.0, "gps_lon": 34.0})
    _row(env, filed, "suggestion", "suggested", 50.0, 8.0)
    client, headers, *_ = env
    r = client.post("/v1/assets/locations/accept", json={"asset_ids": [filed]}, headers=headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "location_exists", r.text
    assert _detail(env, filed)["location"]["status"] == "suggested"
    r = client.post("/v1/assets/locations/accept", json={"asset_ids": [filed], "replace": "all"}, headers=headers)
    assert r.status_code == 200 and r.json()["accepted"] == [filed]
    assert filed in _query(env, "near:50,8,1")
