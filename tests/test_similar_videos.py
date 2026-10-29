# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Find similar across photos and videos (GET /v1/similar, POST /v1/similar/search-by-vector): a video is compared
by its scenes' pictures (video_scenes.likeness, src/producers/scene_likeness), photos by their vectors, both CLIP's
with one model; a video found says where its scene most alike starts (at_ms). How scenes' vectors are made and kept:
tests/test_scene_likeness.py."""

from __future__ import annotations

import pytest

from src.producers.clip import CLIP_MODEL_ID
from src.server.api.routers.similarity import MOST_SOURCE_SCENES
from tests.derived import saved
from tests.scenes import Scene
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _ingest_with, _sha, _want
from tests.test_reconciler import _library
from tests.test_scene_likeness import MODEL, _save, _video

pytestmark = pytest.mark.slow


def _toward(*first: float) -> list[float]:
    """A CLIP-long vector (512 numbers) pointing where its first numbers say."""
    return [*first, *[0.0] * (512 - len(first))]


BEACH, NEAR_BEACH, NEARLY_BEACH = _toward(1, 0, 0), _toward(0.95, 0.05, 0), _toward(0.8, 0.2, 0)
CASTLE, NEAR_CASTLE = _toward(0, 1, 0), _toward(0.05, 0.95, 0)
FIREWORKS, NEAR_FIREWORKS = _toward(0, 0, 1), _toward(0, 0.05, 0.95)


def _photo(lib, name: str, vector: list[float]) -> str:
    """A photo with its CLIP vector, as the photos' producer saves it."""
    client, headers, *_ = lib
    sha = _sha()
    clip = _ingest_with(lib, name, sha)
    r = saved(client, "embeddings", clip, headers=headers, json={
        "model_id": CLIP_MODEL_ID, "model_version": MODEL, "vector": vector,
        "lineage": _want(lib, "lumiverb.image.likeness", sha)})
    assert r.status_code == 200, r.text
    return clip


def _filmed(lib, name: str, vectors: list[list[float]]) -> str:
    """A video of a scene a second for each vector, each scene's picture embedded as that vector."""
    vid, sha, scene_ids = _video(lib, [Scene(i * 1000, (i + 1) * 1000, f"scene {i}") for i in range(len(vectors))],
                                 name)
    for scene_id, vector in zip(scene_ids, vectors, strict=True):
        r = _save(lib, vid, sha, [scene_id], vector)
        assert r.status_code == 200, r.text
    return vid


def _similar(lib, clip: str, **params) -> dict:
    client, headers, library_id, *_ = lib
    r = client.get("/v1/similar", params={"asset_id": clip, "library_id": library_id, **params}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _found(said: dict) -> list[tuple[str, int | None]]:
    """What was found, most alike first: each clip and where in it (a video's)."""
    return [(h["asset_id"], h["at_ms"]) for h in said["hits"]]


def test_a_photo_finds_videos_by_the_scene_most_like_it(env):
    lib = _library(env, "SimilarVideosPhoto")
    beach = _photo(lib, "beach.jpg", BEACH)
    nearly = _photo(lib, "nearly.jpg", NEARLY_BEACH)
    trip = _filmed(lib, "trip.mov", [CASTLE, NEAR_BEACH, FIREWORKS])
    show = _filmed(lib, "show.mov", [FIREWORKS])

    found = _found(_similar(lib, beach))
    assert found[:2] == [(trip, 1000), (nearly, None)]  # the trip's beach a second in; then the photo
    assert (show, 0) in found[2:] and beach not in dict(found)


def test_a_video_finds_photos_and_videos_like_any_of_its_scenes(env):
    lib = _library(env, "SimilarVideosVideo")
    walk = _filmed(lib, "walk.mov", [BEACH, CASTLE])
    shore = _photo(lib, "shore.jpg", NEAR_BEACH)
    keep = _photo(lib, "keep.jpg", NEAR_CASTLE)
    night = _filmed(lib, "night.mov", [FIREWORKS, NEAR_CASTLE])
    lone = _photo(lib, "fireworks.jpg", FIREWORKS)

    said = _similar(lib, walk)
    assert said["embedding_available"] is True
    found = dict(_found(said))
    assert walk not in found  # never itself
    assert list(found)[:3] == [shore, keep, night] or list(found)[:3] == [shore, night, keep]
    assert found[night] == 1000 and found[shore] is None  # night's castle, a second in
    assert list(found).index(lone) > list(found).index(night)  # nothing of the walk looks like fireworks


def test_a_long_videos_scenes_are_compared_from_its_first_to_its_last(env):
    lib = _library(env, "SimilarVideosLong")
    long = _filmed(lib, "long.mov", [CASTLE] * (MOST_SOURCE_SCENES + 2) + [FIREWORKS])
    finale = _photo(lib, "finale.jpg", NEAR_FIREWORKS)
    tower = _photo(lib, "tower.jpg", _toward(0.3, 0.7, 0))  # like its castle, less than the finale like its last
    assert [clip for clip, _ in _found(_similar(lib, long))] == [finale, tower]


def test_a_video_whose_scenes_arent_embedded_has_nothing_to_compare(env):
    lib = _library(env, "SimilarVideosNone")
    _photo(lib, "any.jpg", BEACH)
    vid, _, _ = _video(lib, [Scene(0, 1000, "a pier")])
    said = _similar(lib, vid)
    assert said["embedding_available"] is False and said["hits"] == []


def test_only_photos_when_only_images_are_asked_for(env):
    lib = _library(env, "SimilarVideosImages")
    beach = _photo(lib, "beach.jpg", BEACH)
    nearly = _photo(lib, "nearly.jpg", NEARLY_BEACH)
    _filmed(lib, "trip.mov", [NEAR_BEACH])
    assert _found(_similar(lib, beach, asset_types="image")) == [(nearly, None)]


def test_a_trashed_video_isnt_found(env):
    lib = _library(env, "SimilarVideosTrash")
    client, headers, *_ = lib
    beach = _photo(lib, "beach.jpg", BEACH)
    trip = _filmed(lib, "trip.mov", [NEAR_BEACH])
    assert trip in dict(_found(_similar(lib, beach)))
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [trip], "reason": "user"}, headers=headers)
    assert r.status_code == 200, r.text
    assert trip not in dict(_found(_similar(lib, beach)))


def test_a_vector_sent_finds_videos_by_their_scenes_and_another_models_none(env):
    lib = _library(env, "SimilarVideosVector")
    client, headers, library_id, *_ = lib
    trip = _filmed(lib, "trip.mov", [CASTLE, NEAR_BEACH])
    nearly = _photo(lib, "nearly.jpg", NEARLY_BEACH)

    r = client.post("/v1/similar/search-by-vector", headers=headers, json={
        "library_id": library_id, "vector": BEACH, "model_id": CLIP_MODEL_ID, "model_version": MODEL})
    assert r.status_code == 200, r.text
    assert [(h["asset_id"], h["at_ms"]) for h in r.json()["hits"]] == [(trip, 1000), (nearly, None)]

    # Another model's vector (another length) is compared with nothing of CLIP's.
    r = client.post("/v1/similar/search-by-vector", headers=headers, json={
        "library_id": library_id, "vector": [1.0] * 768, "model_id": CLIP_MODEL_ID, "model_version": "ViT-L-14-openai"})
    assert r.status_code == 200, r.text
    assert r.json()["hits"] == []
