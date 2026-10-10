# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Scenes are found again when their settings change (ADR-016 phase 4).

A clip whose scenes were found another way is started over: the scheduler
asks POST /v1/video/{id}/chunks with redo and how it will find them, and
the server drops the clip's scenes, their descriptions, images and search
entries, so new ones are found and described. Scenes hold no human data.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from src.server.search.quickwit_client import QuickwitClient
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha, _want
from tests.test_reconciler import _counts, _library
from tests.test_redo_on_change import _due

pytestmark = pytest.mark.slow

OLD = "0ld5ce7e5"  # settings nothing finds scenes with now


def _find(lib, vid: str, made: dict, scenes: list[tuple[int, int]]) -> None:
    """Find the clip's scenes as `made` says, through the chunk API the indexer uses."""
    client, headers, *_ = lib
    while True:
        work = client.get(f"/v1/video/{vid}/chunks/next", headers=headers)
        if work.status_code == 204:
            return
        assert work.status_code == 200, work.text
        w = work.json()
        body = {"worker_id": w["worker_id"], "next_anchor_phash": None, "next_scene_start_ms": None,
                "lineage": made, "scenes": [
                    {"scene_index": i, "start_ms": a, "end_ms": b, "rep_frame_ms": a, "keep_reason": "phash"}
                    for i, (a, b) in enumerate(scenes) if w["chunk_index"] == 0]}
        r = client.post(f"/v1/video/chunks/{w['chunk_id']}/complete", json=body, headers=headers)
        assert r.status_code == 200, r.text


def _start(lib, vid: str, **body):
    client, headers, *_ = lib
    return client.post(f"/v1/video/{vid}/chunks", json={"duration_sec": 30.0, **body}, headers=headers)


def _video_found_the_old_way(lib) -> tuple[str, str]:
    """A video whose two scenes were found with old settings, each described, each with its image."""
    client, headers, library_id, storage, tenant_id, _ = lib
    sha = _sha()
    vid = _ingest_with(lib, "v.mov", sha, None, media_type="video")
    with _db(lib) as s:
        s.execute(text("UPDATE assets SET duration_sec = 30, analysis_proxy_key = 'k' WHERE asset_id = :a"),
                  {"a": vid})
        s.commit()
    assert _start(lib, vid).status_code == 200
    old = {**_want(lib, "scenes", sha), "settings_hash": OLD}
    _find(lib, vid, old, [(0, 12000), (12000, 30000)])
    for scene in client.get(f"/v1/video/{vid}/scenes", headers=headers).json()["scenes"]:
        storage.write(storage.scene_rep_key(tenant_id, library_id, vid, scene["rep_frame_ms"]), b"jpeg")
        r = client.patch(f"/v1/video/scenes/{scene['scene_id']}", json={
            "model_id": "m", "model_version": "1", "description": "a dog", "tags": [],
            "lineage": _want(lib, "scene_vision", sha)}, headers=headers)
        assert r.status_code == 200, r.text
    assert _counts(lib, "scenes")["stale"] == 1 and _counts(lib, "scene_vision")["current"] == 1
    return vid, sha


def _scenes(lib, vid: str) -> list[dict]:
    client, headers, *_ = lib
    return client.get(f"/v1/video/{vid}/scenes", headers=headers).json()["scenes"]


def _lineage(lib, vid: str) -> set[str]:
    with _db(lib) as s:
        return {r[0] for r in s.execute(text("SELECT artifact FROM artifact_lineage WHERE asset_id = :a"
                                             " AND artifact IN ('scenes', 'scene_vision')"), {"a": vid})}


def test_scenes_can_be_found_again_and_their_settings_changed(env):
    p = {x["artifact"]: x for x in env[0].get("/v1/producers", params={"counts": "false"},
                                              headers=env[1]).json()["producers"]}["scenes"]
    assert p["redoable"] is True and p["why_not"] is None
    fields = {f["key"]: f for f in p["fields"]}
    assert {k for k, f in fields.items() if not f["fixed"]} == {
        "phash_threshold", "temporal_ceiling_sec", "debounce_sec", "frame_width"}
    assert fields["frames"]["fixed"] and fields["phash_hash_size"]["fixed"]
    assert fields["phash_threshold"]["maximum"] < 256  # a 16 × 16 hash has 256 bits


def test_a_redo_starts_the_clip_over_and_new_scenes_are_found_and_described(env, monkeypatch):
    lib = _library(env, "SceneRedo")
    client, headers, library_id, storage, tenant_id, _ = lib
    vid, sha = _video_found_the_old_way(lib)
    old_images = [storage.abs_path(storage.scene_rep_key(tenant_id, library_id, vid, s["rep_frame_ms"]))
                  for s in _scenes(lib, vid)]
    assert all(p.exists() for p in old_images)
    dropped: list[list[str]] = []
    monkeypatch.setattr(QuickwitClient, "delete_scene_index_documents_by_asset_ids",
                        lambda self, tenant, ids: dropped.append(list(ids)))
    assert _due(lib, "redo_scenes") == [vid]

    new = _want(lib, "scenes", sha)
    r = _start(lib, vid, redo=True, lineage=new)
    assert r.status_code == 200 and r.json()["already_initialized"] is False, r.text
    assert _scenes(lib, vid) == [] and _lineage(lib, vid) == set()
    assert not any(p.exists() for p in old_images) and dropped == [[vid]]
    assert _counts(lib, "scenes")["missing"] == 1  # due as missing now, first among its kind
    _find(lib, vid, new, [(0, 30000)])
    assert len(_scenes(lib, vid)) == 1 and _counts(lib, "scenes")["current"] == 1
    assert _counts(lib, "scene_vision")["missing"] == 1  # the new scene is described next


def test_a_redo_sent_again_changes_nothing(env):
    lib = _library(env, "SceneRedoTwice")
    vid, sha = _video_found_the_old_way(lib)
    new = _want(lib, "scenes", sha)
    assert _start(lib, vid, redo=True, lineage=new).status_code == 200
    # Started over and under way: a second redo leaves the chunks to finish.
    client, headers, *_ = lib
    assert client.get(f"/v1/video/{vid}/chunks/next", headers=headers).status_code == 200
    r = _start(lib, vid, redo=True, lineage=new)
    assert r.status_code == 200 and r.json()["already_initialized"] is True, r.text
    # Found this way already: nothing to start over.
    with _db(lib) as s:
        s.execute(text("UPDATE video_index_chunks SET status = 'pending', worker_id = NULL WHERE asset_id = :a"),
                  {"a": vid})
        s.commit()
    _find(lib, vid, new, [(0, 30000)])
    r = _start(lib, vid, redo=True, lineage=new)
    assert r.status_code == 200 and r.json()["already_initialized"] is True, r.text
    assert len(_scenes(lib, vid)) == 1 and _lineage(lib, vid) == {"scenes"}


def test_a_redo_must_say_how_the_scenes_will_be_found(env):
    lib = _library(env, "SceneRedoSays")
    vid, _ = _video_found_the_old_way(lib)
    r = _start(lib, vid, redo=True)
    assert r.status_code == 422 and r.json()["error"]["code"] == "lineage_required", r.text
    assert len(_scenes(lib, vid)) == 2  # nothing dropped


def test_new_scene_settings_ask_naming_the_scene_descriptions_and_redo_the_clips(env):
    lib = _library(env, "SceneSettings")
    vid, _ = _video_found_the_old_way(lib)
    client, headers, *_ = lib
    # Its old scenes are stale already (OLD); make them current with today's settings first.
    with _db(lib) as s:
        want = client.get("/v1/producers", params={"counts": "false"}, headers=headers).json()["producers"]
        hash_now = next(p["settings_hash"] for p in want if p["artifact"] == "scenes")
        s.execute(text("UPDATE artifact_lineage SET settings_hash = :h WHERE asset_id = :a AND artifact = 'scenes'"),
                  {"h": hash_now, "a": vid})
        s.commit()
    assert _counts(lib, "scenes")["current"] == 1
    try:
        r = client.put("/v1/producers/scenes/settings", json={"settings": {"phash_threshold": 30}}, headers=headers)
        assert r.status_code == 409, r.text
        assert "scene descriptions" in r.json()["error"]["message"] and r.json()["error"]["details"]["clips"] >= 1
        r = client.put("/v1/producers/scenes/settings", json={"settings": {"phash_threshold": 30}, "redo": True},
                       headers=headers)
        assert r.status_code == 200, r.text
        assert _due(lib, "redo_scenes") == [vid]
    finally:
        client.put("/v1/producers/scenes/settings", json={"settings": {"phash_threshold": None}, "redo": True},
                   headers=headers)


def test_a_redo_with_settings_that_arent_the_accounts_now_starts_nothing_over(env):
    """A scheduler that read the settings before they changed again: its redo
    would record settings already stale and be done twice."""
    lib = _library(env, "SceneRedoOld")
    vid, sha = _video_found_the_old_way(lib)
    r = _start(lib, vid, redo=True, lineage={**_want(lib, "scenes", sha), "settings_hash": "n0tn0w"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "settings_changed", r.text
    assert len(_scenes(lib, vid)) == 2


def test_only_editors_start_a_clip_over(env):
    from tests.test_archive_trash_safety import _key_with_role

    lib = _library(env, "SceneRedoViewer")
    vid, sha = _video_found_the_old_way(lib)
    client, *_ = lib
    r = client.post(f"/v1/video/{vid}/chunks", json={"duration_sec": 30.0, "redo": True,
                                                      "lineage": _want(lib, "scenes", sha)},
                    headers=_key_with_role(env, "viewer"))
    assert r.status_code == 403, r.text
    assert len(_scenes(lib, vid)) == 2


def test_a_chunk_from_before_the_start_over_saves_nothing(env):
    lib = _library(env, "SceneRedoLate")
    client, headers, *_ = lib
    vid, sha = _video_found_the_old_way(lib)
    old = {**_want(lib, "scenes", sha), "settings_hash": OLD}
    # An old run's chunk, claimed again before the clip was started over.
    with _db(lib) as s:
        s.execute(text("UPDATE video_index_chunks SET status = 'pending', worker_id = NULL WHERE asset_id = :a"),
                  {"a": vid})
        s.commit()
    w = client.get(f"/v1/video/{vid}/chunks/next", headers=headers).json()
    with _db(lib) as s:  # back to found, as the old run left it
        s.execute(text("UPDATE video_index_chunks SET status = 'completed' WHERE asset_id = :a"), {"a": vid})
        s.commit()
    assert _start(lib, vid, redo=True, lineage=_want(lib, "scenes", sha)).status_code == 200
    r = client.post(f"/v1/video/chunks/{w['chunk_id']}/complete", headers=headers, json={
        "worker_id": w["worker_id"], "next_anchor_phash": None, "next_scene_start_ms": None, "lineage": old,
        "scenes": [{"scene_index": 0, "start_ms": 0, "end_ms": 30000, "rep_frame_ms": 0}]})
    assert r.status_code == 404, r.text
    assert _scenes(lib, vid) == []


def test_describing_a_scene_that_was_dropped_says_its_gone(env):
    lib = _library(env, "SceneGone")
    client, headers, *_ = lib
    vid, sha = _video_found_the_old_way(lib)
    gone = _scenes(lib, vid)[0]["scene_id"]
    assert _start(lib, vid, redo=True, lineage=_want(lib, "scenes", sha)).status_code == 200
    r = client.patch(f"/v1/video/scenes/{gone}", json={
        "model_id": "m", "model_version": "1", "description": "late", "tags": [],
        "lineage": _want(lib, "scene_vision", sha)}, headers=headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "scene_gone", r.text
    assert _lineage(lib, vid) == set()  # nothing recorded for it


def test_a_start_over_deletes_only_images_at_keys_the_server_makes(env, tmp_path):
    """A scene's proxy_key and thumbnail_key are what a client sent: never paths to delete."""
    lib = _library(env, "SceneRedoKeys")
    client, headers, _, storage, *_ = lib
    sha = _sha()
    vid = _ingest_with(lib, "v.mov", sha, None, media_type="video")
    with _db(lib) as s:
        s.execute(text("UPDATE assets SET duration_sec = 30, analysis_proxy_key = 'k' WHERE asset_id = :a"),
                  {"a": vid})
        s.commit()
    outside = tmp_path / "not-a-scene.txt"  # named by an absolute key
    outside.write_text("keep me")
    beside = storage.abs_path("x").parent.parent / f"beside-{vid}.txt"  # named by a ../ key
    beside.write_text("keep me too")
    assert _start(lib, vid).status_code == 200
    w = client.get(f"/v1/video/{vid}/chunks/next", headers=headers).json()
    r = client.post(f"/v1/video/chunks/{w['chunk_id']}/complete", headers=headers, json={
        "worker_id": w["worker_id"], "next_anchor_phash": None, "next_scene_start_ms": None,
        "lineage": {**_want(lib, "scenes", sha), "settings_hash": OLD},
        "scenes": [{"scene_index": 0, "start_ms": 0, "end_ms": 30000, "rep_frame_ms": 0,
                    "thumbnail_key": str(outside), "proxy_key": f"../{beside.name}"}]})
    assert r.status_code == 200, r.text
    assert _start(lib, vid, redo=True, lineage=_want(lib, "scenes", sha)).status_code == 200
    assert _scenes(lib, vid) == [] and outside.read_text() == "keep me" and beside.read_text() == "keep me too"


def test_syncing_a_scene_that_was_dropped_says_its_gone(env):
    lib = _library(env, "SceneGoneSync")
    client, headers, *_ = lib
    vid, sha = _video_found_the_old_way(lib)
    gone = _scenes(lib, vid)[0]["scene_id"]
    assert _start(lib, vid, redo=True, lineage=_want(lib, "scenes", sha)).status_code == 200
    r = client.post(f"/v1/video/scenes/{gone}/sync", json={"asset_id": vid}, headers=headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "scene_gone", r.text
