# ruff: noqa: F811 — pytest fixtures are named as parameters
"""The face cluster cache (GET /v1/faces/clusters).

Reading it never computes it. Upkeep computes it again once something
changed which faces are clustered since it was: a clip trashed, restored,
archived, missing, purged, its library trashed, a face found, named or
un-named; until then the GET says pending. The database says so (triggers
on assets, faces and face_person_matches), so no path that changes those
can forget to.
"""

from __future__ import annotations

import hashlib
import random
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text

from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_archive_by_hand import _sha
from tests.test_lineage_api import _ingest_with, _want
from tests.test_reconciler import _library

pytestmark = pytest.mark.slow


def _embedding(label: str, i: int) -> list[float]:
    rng = random.Random(int(hashlib.sha256(label.encode()).hexdigest()[:8], 16))
    return [rng.uniform(-1.0, 1.0) + i * 0.001 for _ in range(512)]


def _clip_with_faces(lib, name: str, label: str, n: int = 3) -> tuple[str, list[str]]:
    """A photo with n faces of one look (label): they cluster together."""
    client, headers, *_ = lib
    sha = _sha()
    asset_id = _ingest_with(lib, f"{name}.jpg", sha, None)
    r = client.post(f"/v1/assets/{asset_id}/faces", json={
        "detection_model": "insightface", "detection_model_version": "buffalo_l",
        "faces": [{"bounding_box": {"x": 0.1 * i, "y": 0.1, "w": 0.1, "h": 0.1}, "detection_confidence": 0.9,
                   "embedding": _embedding(label, i)} for i in range(n)],
        "lineage": _want(lib, "faces", sha)}, headers=headers)
    assert r.status_code == 201, r.text
    return asset_id, r.json()["face_ids"]


@contextmanager
def _db(env):
    engine = create_engine(env[5])
    try:
        with engine.begin() as conn:
            yield conn
    finally:
        engine.dispose()


class _Load:
    """Upkeep's look at the clusters, then GET /v1/faces/clusters, counting
    the computations upkeep takes (the GET takes none)."""

    def __init__(self, env):
        self.env = env
        self.computed = 0

    def __call__(self) -> set[str]:
        from sqlmodel import Session

        from src.server.api.routers.upkeep import _recluster_if_changed
        from src.server.repository.tenant import FaceRepository

        real = FaceRepository.cluster_cache

        def counted(repo, **kw):
            self.computed += 1
            return real(repo, **kw)

        client, headers, *_ = self.env
        engine = create_engine(self.env[5])
        try:
            with patch.object(FaceRepository, "cluster_cache", counted):
                with Session(engine) as session:
                    _recluster_if_changed(session)
                before = self.computed
                r = client.get("/v1/faces/clusters", params={"limit": 50, "min_cluster_size": 1}, headers=headers)
                assert self.computed == before, "reading the clusters must not compute them"
        finally:
            engine.dispose()
        assert r.status_code == 200, r.text
        assert r.json()["pending"] is False and r.json()["computed_at"]
        with _db(self.env) as conn:
            import json

            cache = json.loads(conn.execute(text(
                "SELECT value FROM system_metadata WHERE key = 'face_clusters_cache'")).scalar())
        return {fid for c in cache["clusters"] for fid in c["face_ids"]}


def _settled(load: _Load) -> set[str]:
    """Clustered faces once the cache is clean (a load computes or serves it)."""
    load()
    before = load.computed
    faces = load()
    assert load.computed == before, "a clean cache must be served, not computed again"
    return faces


def test_a_clean_cache_is_served(env):
    lib = _library(env, "CacheServed")
    _clip_with_faces(lib, "a", "served")
    load = _Load(env)
    load()
    n = load.computed
    for _ in range(3):
        load()
    assert load.computed == n


def test_trashing_and_restoring_one_clip_regroups(env):
    lib = _library(env, "CacheOne")
    client, headers, *_ = lib
    asset_id, faces = _clip_with_faces(lib, "one", "trash-one")
    load = _Load(env)
    assert set(faces) <= _settled(load)

    r = client.delete(f"/v1/assets/{asset_id}", params={"remove_from_projects": True}, headers=headers)
    assert r.status_code == 204, r.text
    n = load.computed
    assert not set(faces) & load()
    assert load.computed == n + 1

    r = client.post(f"/v1/assets/{asset_id}/restore", headers=headers)
    assert r.status_code == 204, r.text
    assert set(faces) <= load()
    assert load.computed == n + 2


def test_trashing_and_restoring_many_regroups(env):
    lib = _library(env, "CacheMany")
    client, headers, *_ = lib
    a, faces_a = _clip_with_faces(lib, "m1", "trash-many")
    b, faces_b = _clip_with_faces(lib, "m2", "trash-many-2")
    load = _Load(env)
    assert set(faces_a + faces_b) <= _settled(load)

    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [a, b], "reason": "user",
                                                     "remove_from_projects": True}, headers=headers)
    assert r.status_code == 200, r.text
    assert not set(faces_a + faces_b) & load()

    r = client.post("/v1/assets/restore", json={"asset_ids": [a, b]}, headers=headers)
    assert r.status_code == 200, r.text
    assert set(faces_a + faces_b) <= load()


def test_missing_archived_and_back_regroups(env):
    lib = _library(env, "CacheArchive")
    client, headers, *_ = lib
    asset_id, faces = _clip_with_faces(lib, "arch", "archive")
    load = _Load(env)
    assert set(faces) <= _settled(load)

    r = client.post("/v1/assets/archive", json={"asset_ids": [asset_id]}, headers=headers)
    assert r.status_code == 200, r.text
    assert not set(faces) & load()
    r = client.post("/v1/assets/unarchive", json={"asset_ids": [asset_id]}, headers=headers)
    assert r.status_code == 200, r.text
    assert set(faces) <= load()

    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [asset_id], "reason": "missing"}, headers=headers)
    assert r.status_code == 200, r.text
    assert not set(faces) & load()


def test_purging_the_trash_regroups(env):
    lib = _library(env, "CachePurge")
    client, headers, *_ = lib
    asset_id, faces = _clip_with_faces(lib, "purge", "purge")
    r = client.delete(f"/v1/assets/{asset_id}", params={"remove_from_projects": True}, headers=headers)
    assert r.status_code == 204, r.text
    load = _Load(env)
    assert not set(faces) & _settled(load)
    n = load.computed

    r = client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [asset_id], "remove_from_projects": True},
                       headers=headers)
    assert r.status_code == 200, r.text
    load()
    assert load.computed == n + 1


def test_trashing_a_library_regroups(env):
    lib = _library(env, "CacheLibrary")
    client, headers, library_id, *_ = lib
    _, faces = _clip_with_faces(lib, "lib", "library")
    load = _Load(env)
    assert set(faces) <= _settled(load)

    r = client.request("DELETE", f"/v1/libraries/{library_id}", json={"remove_from_projects": True},
                       headers=headers)
    assert r.status_code == 204, r.text
    assert not set(faces) & load()


def test_new_faces_and_naming_regroup(env):
    lib = _library(env, "CacheFaces")
    client, headers, *_ = lib
    load = _Load(env)
    _settled(load)
    _, faces = _clip_with_faces(lib, "new", "new-faces")
    assert set(faces) <= load()

    r = client.post("/v1/people", json={"display_name": "Cache Person", "face_ids": faces}, headers=headers)
    assert r.status_code in (200, 201), r.text
    assert not set(faces) & load()


def test_any_write_to_a_clips_sight_regroups(env):
    """The database marks it, so a path the code forgot can't serve stale clusters."""
    lib = _library(env, "CacheAnyPath")
    asset_id, faces = _clip_with_faces(lib, "raw", "any-path")
    load = _Load(env)
    assert set(faces) <= _settled(load)
    with _db(env) as conn:
        conn.execute(text("UPDATE assets SET deleted_at = now(), deleted_reason = 'user' WHERE asset_id = :a"),
                     {"a": asset_id})
    assert not set(faces) & load()


def test_writes_that_leave_faces_as_they_are_keep_the_cache(env):
    lib = _library(env, "CacheKeep")
    asset_id, _ = _clip_with_faces(lib, "keep", "keep")
    load = _Load(env)
    _settled(load)
    n = load.computed
    with _db(env) as conn:
        conn.execute(text("UPDATE assets SET rel_path = rel_path WHERE asset_id = :a"), {"a": asset_id})
    load()
    assert load.computed == n


def test_reading_never_computes_and_says_pending(env):
    """GET /v1/faces/clusters serves the last result: computed_at and pending say how old."""
    from src.server.repository.tenant import FaceRepository

    lib = _library(env, "CachePending")
    client, headers, *_ = lib
    load = _Load(env)
    _settled(load)
    _, faces = _clip_with_faces(lib, "pending", "pending")
    with patch.object(FaceRepository, "cluster_cache", side_effect=AssertionError("computed in a GET")):
        r = client.get("/v1/faces/clusters", params={"limit": 50, "min_cluster_size": 1}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["pending"] is True
    assert r.json()["computed_at"]
    assert set(faces) <= load()
