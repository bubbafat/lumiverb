# ruff: noqa: F811 — pytest fixtures are named as parameters
"""A search understood, end to end (src/server/search/understand.py): GET /v1/query finds a clip by who's
recognised in it, where it was and what kind it is, where the words say so, in the real database. The
places plug-in is the one thing stood in for: what it answers is its own protocol's, tested apart
(tests/test_geonames_program.py)."""

from __future__ import annotations

import uuid
from urllib.parse import quote

import pytest
from sqlalchemy import text

from src.server.models.tenant import Face
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_text_search import _clip, _db, _describe, _library, _note

pytestmark = pytest.mark.slow

DISNEYLAND = (33.8121, -117.9190)  # where the clips were taken: Disneyland, by its GPS
DCA = {"name": "California Adventure", "lat": 33.8061, "lon": -117.9209}  # under a kilometre away
PARIS = (48.8566, 2.3522)


@pytest.fixture
def places(monkeypatch):
    """The places plug-in: California Adventure, and a Beach in North Dakota nobody's been to."""
    from src.server import plugin_lookups

    known = {"california adventure": [DCA], "beach": [{"name": "Beach", "lat": 46.918, "lon": -104.004}]}

    def find(session, q, limit):
        return [{**p, "matched": None} for p in known.get(q, [])], [], []

    monkeypatch.setattr(plugin_lookups, "find_places", find)


def _at(env, clip: str, where: tuple[float, float]) -> None:
    """Its file's own GPS, as a scan reads it."""
    with _db(env) as s:
        s.execute(text("UPDATE assets SET gps_lat = :lat, gps_lon = :lon WHERE asset_id = :a"),
                  {"lat": where[0], "lon": where[1], "a": clip})
        s.commit()


def _face(env, clip: str) -> str:
    """A face found in the clip, not yet anyone's."""
    face_id = f"face_{uuid.uuid4().hex[:12]}"
    with _db(env) as s:
        s.add(Face(face_id=face_id, asset_id=clip, bounding_box_json={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
                   detection_confidence=0.9))
        s.commit()
    return face_id


def _named(env, name: str, face_ids: list[str]) -> str:
    client, headers, *_ = env
    r = client.post("/v1/people", json={"display_name": name, "face_ids": face_ids}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["person_id"]


def _search(env, lib: str, *terms: str) -> dict:
    client, headers, *_ = env
    r = client.get(f"/v1/query?{'&'.join(f'f=query:{quote(t)}' for t in terms)}&f=library:{lib}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _found(env, lib: str, *terms: str) -> set[str]:
    return {i["asset_id"] for i in _search(env, lib, *terms)["items"]}


@pytest.fixture
def trip(env, places):
    """A day at the parks: Sue dancing (a video), a woman dancing nobody's named (a video), Sue dancing in a
    photo, Sue at the fireworks (a video), Sue dancing in Paris (a video), and a video whose note names Sue."""
    lib = _library(env)
    clips = {}
    for name, media, description, where, sue in (
            ("sue_dancing", "video", "a woman dancing", DISNEYLAND, True),
            ("someone_dancing", "video", "a woman dancing", DISNEYLAND, False),
            ("sue_dancing_photo", "image", "a woman dancing", DISNEYLAND, True),
            ("sue_fireworks", "video", "fireworks over a castle", DISNEYLAND, True),
            ("sue_in_paris", "video", "a woman dancing", PARIS, True),
            ("noted", "video", "a woman dancing", DISNEYLAND, False)):
        clips[name] = _clip(env, lib, f"Disneyland/{name}.{'mp4' if media == 'video' else 'jpg'}", media)
        _describe(env, clips[name], description)
        _at(env, clips[name], where)
    _note(env, clips["noted"], "Sue, after lunch")
    sue = _named(env, "Susan Horvick", [_face(env, c) for n, c in clips.items() if n.startswith("sue")])
    return lib, clips, sue


def test_a_video_of_sue_dancing_at_california_adventure(env, trip):
    lib, clips, _ = trip
    said = _search(env, lib, "a video of Sue dancing at California Adventure")
    # Her face, under a kilometre from California Adventure (its GPS nearer Disneyland), a video,
    # described as a woman dancing; and the one whose note names her, as the words always found it.
    assert {i["asset_id"] for i in said["items"]} == {clips["sue_dancing"], clips["noted"]}
    assert said["understood"] == [
        {"kind": "kind", "words": "video", "label": "videos"},
        {"kind": "person", "words": "sue", "label": "Susan Horvick"},
        {"kind": "place", "words": "california adventure", "label": "California Adventure"},
        {"kind": "words", "words": "dancing", "label": "dancing"}]


def test_a_places_name_matches_the_words_whole(env, trip):
    lib, clips, _ = trip
    elsewhere = _clip(env, lib, "Florida/adventure.mp4", "video")  # a video of an adventure in California, not there
    _describe(env, elsewhere, "a woman dancing on an adventure through California")
    _at(env, elsewhere, (28.3852, -81.5639))
    sue = _search(env, lib, "Sue")["understood"]
    assert sue and sue[0]["label"] == "Susan Horvick"
    assert elsewhere not in _found(env, lib, "dancing at California Adventure")


def test_every_part_must_match(env, trip):
    lib, clips, _ = trip
    assert _found(env, lib, "Sue dancing") == {clips["sue_dancing"], clips["sue_dancing_photo"],
                                               clips["sue_in_paris"], clips["noted"]}
    assert _found(env, lib, "Sue at California Adventure") == {
        clips["sue_dancing"], clips["sue_dancing_photo"], clips["sue_fireworks"], clips["noted"]}
    assert _found(env, lib, "photos of Sue") == {clips["sue_dancing_photo"]}
    assert _found(env, lib, "Sue fireworks") == {clips["sue_fireworks"]}


def test_a_person_matches_by_who_is_recognised_or_by_the_words(env, trip):
    lib, clips, _ = trip
    by_face = {clips[n] for n in ("sue_dancing", "sue_dancing_photo", "sue_fireworks", "sue_in_paris")}
    assert _found(env, lib, "Susan") == by_face | {clips["noted"]}  # "Susan" is Sue: her note too
    assert _found(env, lib, "Horvick") == by_face


def test_a_place_nobody_has_been_to_is_a_word(env, trip):
    lib, clips, _ = trip
    said = _search(env, lib, "dancing beach")  # North Dakota's Beach: no clip within 5 km of it
    assert said["understood"] is None
    assert {i["asset_id"] for i in said["items"]} == {
        clips[n] for n in ("sue_dancing", "someone_dancing", "sue_dancing_photo", "sue_in_paris", "noted")}


def test_a_search_with_nothing_understood_is_as_it_was(env, trip):
    lib, clips, _ = trip
    said = _search(env, lib, "fireworks")
    assert said["understood"] is None and {i["asset_id"] for i in said["items"]} == {clips["sue_fireworks"]}


def test_searches_combined_each_understood_must_each_match(env, trip):
    lib, clips, _ = trip
    assert _found(env, lib, "Sue", "dancing at California Adventure") == {
        clips["sue_dancing"], clips["sue_dancing_photo"], clips["noted"]}


def test_a_public_pages_search_reads_only_the_words(env, trip):
    from src.server.api.routers.query import _run_text_search
    from src.server.models.query_filter import SearchTerm

    lib, clips, _ = trip
    said: list[dict] = []
    with _db(env) as session:
        scores, _contexts, _source = _run_text_search(session, [SearchTerm(q="Sue dancing")], [lib], 100,
                                                      public=True, understood=said)
    assert said == []  # no people on a public page: its words, as they always were
    assert set(scores) == {clips[n] for n in ("sue_dancing", "someone_dancing", "sue_dancing_photo",
                                              "sue_in_paris", "noted")}
