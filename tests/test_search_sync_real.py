# ruff: noqa: F811 — pytest fixtures are named as parameters
"""What search finds, written the way the server writes it, in the account's own database (ADR-019).

Each thing a person can find a clip by, saved through the API, then looked
for through GET /v1/query: a clip's words as a person sees them (a person's
description over the machine's, the text read in it, its note, its path), a
scene's description and tags with its times, a transcript's segment with its
times. Saved again, the old words aren't found and the clip's other words
are; a library deleted for good leaves nothing to find.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha, _want
from tests.test_reconciler import _library
from tests.derived import saved

pytestmark = pytest.mark.slow


def _found(lib, words: str, *, everywhere: bool = False) -> dict[str, dict]:
    """What a search finds (in the library, or the whole account): {asset_id: its search context}."""
    client, headers, library_id, *_ = lib
    params = [("f", f"query:{words}")] + ([] if everywhere else [("f", f"library:{library_id}")])
    r = client.get("/v1/query", params=params, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["search_source"] == "postgres"
    return {i["asset_id"]: i["search_context"] for i in r.json()["items"]}


def _hit(lib, clip: str, words: str) -> tuple:
    """Where a search finds the clip: (hit type, start, end, snippet)."""
    ctx = _found(lib, words).get(clip)
    assert ctx is not None, f"{words!r} doesn't find the clip"
    return ctx["hit_type"], ctx["start_ms"], ctx["end_ms"], ctx["snippet"]


def _ok(r) -> None:
    assert r.status_code in (200, 204), r.text


def _describe(lib, clip: str, description: str, tags: list[str]) -> None:
    client, headers, *_ = lib
    _ok(saved(client, "vision", clip, headers=headers, json={
        "model_id": "m", "description": description, "tags": tags, "lineage": _want(lib, "lumiverb.image.description", None)}))


def _correct(lib, clip: str, **body) -> None:
    client, headers, *_ = lib
    _ok(client.patch(f"/v1/assets/{clip}/corrections", json=body, headers=headers))


def _transcribe(lib, clip: str, srt: str) -> None:
    client, headers, *_ = lib
    _ok(client.post(f"/v1/assets/{clip}/transcript", json={"srt": srt, "source": "manual", "language": "en"},
                    headers=headers))


def _scene(lib, vid: str, n: int, start_ms: int, end_ms: int) -> str:
    """A scene the clip shows, found but not described yet."""
    scene_id = f"scn_{vid}_{n}"
    with _db(lib) as s:
        s.execute(text("INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms, rep_frame_ms,"
                       " tags, created_at) VALUES (:id, :a, :n, :st, :en, :st, '[]', now())"),
                  {"id": scene_id, "a": vid, "n": n, "st": start_ms, "en": end_ms})
        s.commit()
    return scene_id


def _describe_scene(lib, scene_id: str, description: str, tags: list[str]) -> None:
    """As the scene describer saves it (PATCH /v1/video/scenes/{id}): searchable once saved."""
    client, headers, *_ = lib
    _ok(client.patch(f"/v1/video/scenes/{scene_id}", headers=headers, json={
        "model_id": "m", "model_version": "1", "description": description, "tags": tags,
        "lineage": _want(lib, "lumiverb.video.scene_descriptions", None)}))


def test_a_clip_is_found_by_every_word_a_person_sees(env):
    client, headers, *_ = env
    lib = _library(env, "FoundEveryWord")
    clip = _ingest_with(lib, "Trips/2024/beach_day.jpg", _sha(), None)
    _describe(lib, clip, "a lighthouse on a rocky point", ["lighthouse", "coast"])
    # A person rewrote the description: search finds their words, not the machine's.
    _correct(lib, clip, description="a quoddy lighthouse in fog")
    _ok(saved(client, "ocr", clip, json={"ocr_text": "WELCOME TO MARBLEHEAD",
                                                    "lineage": _want(lib, "lumiverb.image.text", None)}, headers=headers))
    _ok(client.put(f"/v1/assets/{clip}/note", json={"text": "ask about the zephyrine print"}, headers=headers))

    for words in ("quoddy", "coast", "marblehead", "zephyrine", "beach"):  # description, tags, OCR, note, path
        assert _hit(lib, clip, words) == ("asset", None, None, "a quoddy lighthouse in fog"), words
    assert clip not in _found(lib, "rocky"), "the machine's description, replaced, is still found"


def test_a_scene_is_found_with_its_times(env):
    lib = _library(env, "FoundScene")
    vid = _ingest_with(lib, "Clips/harbor.mov", _sha(), None, media_type="video")
    scene = _scene(lib, vid, 2, 41_000, 52_500)
    assert vid not in _found(lib, "trawler")
    _describe_scene(lib, scene, "a trawler unloading lobster traps", ["boat", "lobster"])
    assert _hit(lib, vid, "trawler") == ("scene", 41_000, 52_500, "a trawler unloading lobster traps")
    assert _hit(lib, vid, "boat")[:3] == ("scene", 41_000, 52_500)  # its tags too


SRT = """1
00:00:01,000 --> 00:00:04,200
Welcome aboard the ferry to Nantucket.

2
00:01:05,500 --> 00:01:09,000
Watch for the cormorants off the jetty.
"""


def test_a_transcript_segment_is_found_with_its_times(env):
    lib = _library(env, "FoundTranscript")
    vid = _ingest_with(lib, "Clips/ferry.mov", _sha(), None, media_type="video")
    _transcribe(lib, vid, SRT)
    assert _hit(lib, vid, "cormorants") == ("transcript", 65_500, 69_000, "Watch for the cormorants off the jetty.")
    assert _hit(lib, vid, "nantucket") == ("transcript", 1_000, 4_200, "Welcome aboard the ferry to Nantucket.")


def test_a_rewritten_description_replaces_the_old_words_and_keeps_the_clips_others(env):
    lib = _library(env, "FoundRewritten")
    vid = _ingest_with(lib, "x/clip.mov", _sha(), None, media_type="video")
    _describe(lib, vid, "zorblax at the beach", [])
    _describe_scene(lib, _scene(lib, vid, 0, 0, 1000), "a trawler unloading", [])
    _transcribe(lib, vid, "1\n00:00:01,000 --> 00:00:02,000\nWatch the cormorants.\n")
    assert vid in _found(lib, "zorblax")

    _correct(lib, vid, description="quillfeather at the beach")  # a person rewrites it

    assert vid not in _found(lib, "zorblax"), "the old description is still found"
    assert vid in _found(lib, "quillfeather")
    assert _hit(lib, vid, "trawler")[0] == "scene"  # its scene stays
    assert _hit(lib, vid, "cormorants")[0] == "transcript"  # and its transcript


def test_a_scene_described_again_replaces_that_scene_only(env):
    lib = _library(env, "FoundSceneAgain")
    vid = _ingest_with(lib, "x/scenes.mov", _sha(), None, media_type="video")
    one, two = _scene(lib, vid, 0, 0, 999), _scene(lib, vid, 1, 1000, 1999)
    _describe_scene(lib, one, "a pelican on a piling", [])
    _describe_scene(lib, two, "a heron in the reeds", [])
    _describe_scene(lib, one, "a cormorant drying its wings", [])  # described again

    assert vid not in _found(lib, "pelican"), "the scene's old description is still found"
    assert _hit(lib, vid, "cormorant")[:3] == ("scene", 0, 999)
    assert _hit(lib, vid, "heron")[:3] == ("scene", 1000, 1999)  # the clip's other scene stays


def test_a_library_deleted_for_good_leaves_nothing_found(env):
    client, headers, *_ = env
    lib = _library(env, "FoundPurged")
    photo = _ingest_with(lib, "a.jpg", _sha(), None)
    _describe(lib, photo, "a gannet colony", [])
    vid = _ingest_with(lib, "b.mov", _sha(), None, media_type="video")
    _describe_scene(lib, _scene(lib, vid, 0, 0, 1000), "a gannet diving", [])
    _transcribe(lib, vid, "1\n00:00:01,000 --> 00:00:02,000\nThe gannets are back.\n")
    assert {photo, vid} <= set(_found(lib, "gannet", everywhere=True))

    assert client.delete(f"/v1/libraries/{lib[2]}", headers=headers).status_code == 204
    assert not {photo, vid} & set(_found(lib, "gannet", everywhere=True)), "a trashed library's clips are found"
    r = client.post("/v1/libraries/empty-trash", json={"library_ids": [lib[2]]}, headers=headers)
    assert r.status_code == 200 and r.json()["deleted"] == 1, r.text

    assert not {photo, vid} & set(_found(lib, "gannet", everywhere=True))
    with _db(env) as s:
        for table in ("asset_search", "video_scenes", "transcript_segments"):
            left = s.execute(text(f"SELECT count(*) FROM {table} WHERE asset_id = ANY(:ids)"),
                             {"ids": [photo, vid]}).scalar()
            assert left == 0, f"{table} still holds the deleted library's clips"
