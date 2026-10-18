"""The Place names plug-in's producer (ADR-021) against the real API and
database, with a local stand-in for GeoNames: nothing due while the plug-in
is off; each clip with a location (the file's GPS, a person's, a guess)
named with the nearest town within its radius, or none that near, and the
landmark it's at (within its kind's reach, for a location as sure); named
again when its location changes, the old name not shown meanwhile; a name
found for a location the clip no longer shows refused; names kept when the
plug-in is turned off; nothing on a public page; the scheduler's routes
an admin's."""

from __future__ import annotations

import io
import json
import os
import time

import pytest
from sqlalchemy import text

from src.plugins import registry
from src.producers.place.work import Name
from src.server.config import get_settings
from src.server.database import get_control_session
from tests.machine_lineage import made
from tests.producer_fakes import FakeAccount, due, run_job, scheduler_client
from tests.test_geonames import (
    CHAMP_DE_MARS,
    EIFFEL,
    MAGIC_KINGDOM,
    geonames_site,  # noqa: F401 — the stand-in for download.geonames.org
)
from tests.test_library_access import _tenant_of
from tests.test_scans_api import _library, env  # noqa: F401 — the shared fixture

pytestmark = pytest.mark.slow
GEONAMES = "geonames"
PORT_OF_SPAIN = (10.66, -61.52)  # 1 km or so from the town's point
PARIS = (48.86, 2.35)
MID_ATLANTIC = (20.0, -40.0)


@pytest.fixture(autouse=True)
def _fresh(env, geonames_site, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "lumiverb_geonames_url", geonames_site)
    yield
    env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": False}, headers=env.admin)


def _turn_on(env) -> None:  # noqa: F811
    version = registry()[GEONAMES].acknowledgment.version
    r = env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": True, "acknowledged": version}, headers=env.admin)
    assert r.status_code == 200, r.text
    for _ in range(300):
        data = next(p for p in env.client.get("/v1/plugins", headers=env.admin).json()["plugins"]
                    if p["plugin_id"] == GEONAMES)["data"]
        if data["state"] == "ready":
            return
        time.sleep(0.05)
    raise AssertionError(f"the place list never came: {data}")


def _clip(env, lib: str, rel_path: str, gps: tuple[float, float] | None = None,  # noqa: F811
          accuracy_m: float | None = None) -> str:
    """A clip as the scan ingests it, with GPS from its file (as sure as accuracy_m says) or none."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (16, 16)).save(buf, format="JPEG")
    buf.seek(0)
    exif = {"content_hash": "sha256:" + os.urandom(32).hex(), "taken_at": "2024-06-15T14:00:00+00:00"}
    if gps:
        exif |= {"gps_lat": gps[0], "gps_lon": gps[1]}
    if accuracy_m is not None:
        exif |= {"gps_accuracy_m": accuracy_m}
    r = env.client.post("/v1/ingest", headers=env.admin, files={"proxy": ("p.jpg", buf, "image/jpeg")}, data={
        "library_id": lib, "rel_path": rel_path, "file_size": "10", "media_type": "image",
        "lineage": json.dumps({"proxy": made("proxy"), "capture": made("capture")}), "exif": json.dumps(exif)})
    assert r.status_code == 200, r.text
    return r.json()["asset_id"]


def _due(env, lib: str) -> set[str]:  # noqa: F811
    return {c["asset_id"] for c in due(env.tenant_url, "place", [lib])}


def _run(env, lib: str, kind: str = "place"):  # noqa: F811
    items = due(env.tenant_url, kind, [lib])
    acct = FakeAccount(scheduler_client(_tenant_of(env)))
    outcome = run_job(Name, acct, _tenant_of(env), kind, items)
    assert acct.failures.charged == [], acct.failures.charged
    return outcome


def _place(env, asset_id: str, headers=None) -> dict | None:  # noqa: F811
    r = env.client.get(f"/v1/assets/{asset_id}", headers=headers or env.admin)
    assert r.status_code == 200, r.text
    return r.json()["place"]


def test_nothing_is_named_while_place_names_is_off(env) -> None:  # noqa: F811
    lib = _library(env)
    _clip(env, lib, "a.jpg", PORT_OF_SPAIN)
    assert _due(env, lib) == set()
    r = env.client.get("/v1/producers", headers=env.admin)
    row = next(p for p in r.json()["producers"] if p["artifact"] == "place")
    assert row["plugin"] == {"plugin_id": GEONAMES, "title": "Place names (GeoNames)", "on": False}


def test_clips_with_a_location_are_named_with_the_nearest_town(env) -> None:  # noqa: F811
    lib = _library(env)
    near = _clip(env, lib, "near.jpg", PORT_OF_SPAIN)
    sea = _clip(env, lib, "sea.jpg", MID_ATLANTIC)
    nowhere = _clip(env, lib, "nowhere.jpg")
    placed = _clip(env, lib, "placed.jpg")
    r = env.client.put("/v1/assets/locations", json={"asset_ids": [placed], "lat": PARIS[0], "lon": PARIS[1]},
                       headers=env.editor)
    assert r.status_code == 200, r.text
    _turn_on(env)
    assert _due(env, lib) == {near, sea, placed}  # never the clip with no location

    assert _run(env, lib) is None
    assert _due(env, lib) == set()
    named = _place(env, near)
    assert named["label"] == "Port of Spain, Trinidad and Tobago" and named["place_id"] == "geonames:3573890"
    assert 0 < named["distance_m"] < 3000
    assert named["credit"] == {"text": "GeoNames", "url": "https://www.geonames.org/"}
    assert _place(env, placed)["label"] == "Paris, Île-de-France, France"  # a person's location, named too
    assert _place(env, sea)["label"] is None  # no town within 25 km
    assert _place(env, nowhere) is None
    with env.db.connect() as conn:
        outcomes = dict(conn.execute(text(
            "SELECT asset_id, outcome FROM artifact_lineage WHERE artifact = 'place'")).all())
    assert outcomes[near] == "ok" and outcomes[sea] == "empty"

    # Others see it; a public page never does.
    assert _place(env, near, env.viewer)["label"] == "Port of Spain, Trinidad and Tobago"
    public = env.client.get(f"/v1/assets/{near}", params={"public_library_id": lib})
    assert "Port of Spain" not in public.text

    # Turned off: names stay, nothing more is named.
    env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": False}, headers=env.admin)
    assert _place(env, near)["label"] == "Port of Spain, Trinidad and Tobago"
    late = _clip(env, lib, "late.jpg", PORT_OF_SPAIN)
    assert _due(env, lib) == set()
    r = env.client.post("/v1/places/nearest", json={"asset_ids": [late], "within_km": 25}, headers=env.admin)
    assert r.status_code == 409 and r.json()["error"]["code"] == "plugin_off"


def test_a_clip_whose_location_changes_is_named_again(env) -> None:  # noqa: F811
    lib = _library(env)
    clip = _clip(env, lib, "moved.jpg", PORT_OF_SPAIN)
    _turn_on(env)
    _run(env, lib)
    assert _place(env, clip)["label"] == "Port of Spain, Trinidad and Tobago"

    # A name found for where it was, saved after it moved: refused, nothing written.
    found = env.client.post("/v1/places/nearest", json={"asset_ids": [clip], "within_km": 25},
                            headers=env.admin).json()["clips"][clip]
    r = env.client.put("/v1/assets/locations", json={"asset_ids": [clip], "lat": PARIS[0], "lon": PARIS[1],
                                                     "replace": "all"}, headers=env.editor)
    assert r.status_code == 200, r.text
    assert _place(env, clip) is None  # the old name isn't shown for the new location
    assert clip in _due(env, lib)
    item = {"asset_id": clip, "place_id": found["place"]["place_id"], "distance_m": found["distance_m"],
            "lat": found["lat"], "lon": found["lon"]}
    r = env.client.post("/v1/places/names", json={"items": [item], "lineage": made("place")}, headers=env.admin)
    assert r.status_code == 200 and r.json() == {"saved": 0, "stale": [clip], "gone": []}, r.text

    _run(env, lib)
    assert _place(env, clip)["label"] == "Paris, Île-de-France, France"
    assert _due(env, lib) == set()
    # Cleared, the file's GPS shows again: named from it again.
    assert env.client.request("DELETE", "/v1/assets/locations", json={"asset_ids": [clip]},
                              headers=env.editor).status_code == 200
    assert clip in _due(env, lib)
    _run(env, lib)
    assert _place(env, clip)["label"] == "Port of Spain, Trinidad and Tobago"


def test_a_clip_placed_by_a_guess_is_named_too(env) -> None:  # noqa: F811
    lib = _library(env)
    assert env.client.patch(f"/v1/libraries/{lib}", json={"infer_location": True},
                            headers=env.admin).status_code == 200
    clip = _clip(env, lib, "guessed.jpg")
    _turn_on(env)
    assert clip not in _due(env, lib)
    guess = {"guess": {"lat": PORT_OF_SPAIN[0], "lon": PORT_OF_SPAIN[1], "radius_m": 2000, "basis": {}},
             "minutes": 360, "lineage": made("location")}
    r = env.client.put(f"/v1/assets/locations/guess/{clip}", json=guess, headers=env.admin)
    assert r.json() == {"result": "stored"}, r.text
    assert clip in _due(env, lib)
    _run(env, lib)
    assert _place(env, clip)["label"] == "Port of Spain, Trinidad and Tobago"


def test_the_radius_is_a_setting_that_names_again(env) -> None:  # noqa: F811
    lib = _library(env)
    clip = _clip(env, lib, "edge.jpg", (10.66, -61.95))  # about 47 km west of Port of Spain
    _turn_on(env)
    _run(env, lib)
    assert _place(env, clip)["label"] is None
    try:
        r = env.client.put("/v1/producers/place/settings", json={"settings": {"radius_km": 60}}, headers=env.admin)
        assert r.status_code == 409 and r.json()["error"]["code"] == "redo_on_change", r.text
        r = env.client.put("/v1/producers/place/settings", json={"settings": {"radius_km": 60}, "redo": True},
                           headers=env.admin)
        assert r.status_code == 200, r.text
        # Named with the old radius: remade, after everything else (the scheduler's redo).
        assert clip in {c["asset_id"] for c in due(env.tenant_url, "redo_place", [lib])}
        _run(env, lib, "redo_place")
        named = _place(env, clip)
        assert named["label"] == "Port of Spain, Trinidad and Tobago" and 40_000 < named["distance_m"] < 55_000
    finally:
        env.client.put("/v1/producers/place/settings", json={"settings": {"radius_km": None}, "redo": True},
                       headers=env.admin)


def test_only_the_scheduler_names_places(env) -> None:  # noqa: F811
    lib = _library(env)
    clip = _clip(env, lib, "who.jpg", PORT_OF_SPAIN)
    _turn_on(env)
    for headers in (env.editor, env.viewer):
        assert env.client.post("/v1/places/nearest", json={"asset_ids": [clip], "within_km": 25},
                               headers=headers).status_code == 403
        assert env.client.post("/v1/places/names", json={"items": [], "lineage": made("place")},
                               headers=headers).status_code == 403
    r = env.client.post("/v1/places/nearest", json={"within_km": 25}, headers=env.admin)
    assert r.status_code == 400 and r.json()["error"]["code"] == "scope_required"
    item = {"asset_id": clip, "place_id": "geonames:1", "distance_m": 5, "lat": PORT_OF_SPAIN[0],
            "lon": PORT_OF_SPAIN[1]}
    r = env.client.post("/v1/places/names", json={"items": [item], "lineage": made("place")}, headers=env.admin)
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_place"
    with get_control_session() as session:
        assert session.execute(text("SELECT count(*) FROM geonames_places")).scalar() > 0


def test_a_clip_at_a_landmark_is_named_with_it_and_its_town(env) -> None:  # noqa: F811
    lib = _library(env)
    tower = _clip(env, lib, "tower.jpg", (EIFFEL[0] + 0.0001, EIFFEL[1]))  # 11 m from the tower's point
    lawn = _clip(env, lib, "lawn.jpg", (CHAMP_DE_MARS[0] - 0.0004, CHAMP_DE_MARS[1]))  # 45 m from the park's
    rides = _clip(env, lib, "rides.jpg", (MAGIC_KINGDOM[0] - 0.005, MAGIC_KINGDOM[1]))  # 556 m: in its 1.5 km
    unsure = _clip(env, lib, "unsure.jpg", (EIFFEL[0] + 0.0001, EIFFEL[1]), accuracy_m=400)
    street = _clip(env, lib, "street.jpg", (48.8566, 2.3522))  # central Paris: at none
    _turn_on(env)
    _run(env, lib)

    at_tower = _place(env, tower)
    assert at_tower["label"] == "Paris, Île-de-France, France"
    assert at_tower["landmark"] == {"place_id": "geonames:6254976", "name": "Tour Eiffel",
                                    "label": "Tour Eiffel, Île-de-France, France", "distance_m": 11}
    # Within the tower's 250 m and the park's 1.5 km, the nearer for its reach.
    assert _place(env, lawn)["landmark"]["name"] == "Champ de Mars"
    # No town that near, and a landmark: named with it ("ok", not "empty").
    at_rides = _place(env, rides)
    assert at_rides["label"] is None and at_rides["landmark"]["name"] == "Magic Kingdom, Walt Disney World"
    assert at_rides["landmark"]["label"] == "Magic Kingdom, Walt Disney World, Florida, United States"
    # GPS sure to 400 m isn't at a 250 m tower; it is in the park around it.
    assert _place(env, unsure)["landmark"]["name"] == "Champ de Mars"
    assert _place(env, street)["landmark"] is None and _place(env, street)["label"] == "Paris, Île-de-France, France"
    with env.db.connect() as conn:
        outcomes = dict(conn.execute(text(
            "SELECT asset_id, outcome FROM artifact_lineage WHERE artifact = 'place'")).all())
    assert outcomes[rides] == "ok"
    public = env.client.get(f"/v1/assets/{tower}", params={"public_library_id": lib})
    assert "Eiffel" not in public.text


def test_a_guess_is_at_a_landmark_only_when_as_sure(env) -> None:  # noqa: F811
    lib = _library(env)
    assert env.client.patch(f"/v1/libraries/{lib}", json={"infer_location": True},
                            headers=env.admin).status_code == 200
    near, far = _clip(env, lib, "near-guess.jpg"), _clip(env, lib, "far-guess.jpg")
    _turn_on(env)
    for clip, radius in ((near, 1000), (far, 2000)):
        guess = {"guess": {"lat": MAGIC_KINGDOM[0], "lon": MAGIC_KINGDOM[1], "radius_m": radius, "basis": {}},
                 "minutes": 360, "lineage": made("location")}
        r = env.client.put(f"/v1/assets/locations/guess/{clip}", json=guess, headers=env.admin)
        assert r.json() == {"result": "stored"}, r.text
    _run(env, lib)
    assert _place(env, near)["landmark"]["name"] == "Magic Kingdom, Walt Disney World"  # about 1 km: in its 1.5
    assert _place(env, far)["landmark"] is None  # about 2 km: maybe not


def test_a_landmark_that_isnt_in_the_list_is_refused(env) -> None:  # noqa: F811
    lib = _library(env)
    clip = _clip(env, lib, "refused.jpg", EIFFEL)
    _turn_on(env)
    item = {"asset_id": clip, "place_id": None, "landmark_id": "geonames:1", "landmark_distance_m": 5,
            "lat": EIFFEL[0], "lon": EIFFEL[1]}
    r = env.client.post("/v1/places/names", json={"items": [item], "lineage": made("place")}, headers=env.admin)
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_place"
