# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Archive and trash, review round 1: what the trash may delete, and when.

- Emptying the trash deletes only what was shown: one library's trash when
  filtered, never the clips of a trashed library (they go with it).
- The purge re-checks under a row lock: a library, project or clip restored
  (or archived again) meanwhile is never deleted.
- Restoring a clip that was archived before it was trashed puts it back in
  the archive, as restoring a project puts it back as it was.
- Trashing what projects use asks first, from an exclude filter too.
- Shortening the trash days asks first when it would delete things at once.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session

from src.shared.utils import utcnow
from tests.test_analysis_proxy_api import _ingest, env  # noqa: F401 — the shared server fixture


def _sha() -> str:
    return os.urandom(32).hex()


@contextmanager
def _db(env):
    engine = create_engine(env[-1])
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


def _reason(env, asset_id: str):
    with _db(env) as s:
        row = s.execute(text("SELECT deleted_reason, deleted_at IS NOT NULL FROM assets WHERE asset_id = :a"),
                        {"a": asset_id}).first()
    return None if row is None else (row[0] if row[1] else "active")


def _age(env, table: str, column: str, key: str, value: str, days: int) -> None:
    with _db(env) as s:
        s.execute(text(f"UPDATE {table} SET {column} = :t WHERE {key} = :v"),
                  {"t": utcnow() - timedelta(days=days), "v": value})
        s.commit()


def _exists(env, table: str, key: str, value: str) -> bool:
    with _db(env) as s:
        return s.execute(text(f"SELECT 1 FROM {table} WHERE {key} = :v"), {"v": value}).first() is not None


def _trash(env, *ids: str, yes: bool = True):
    client, headers, *_ = env
    return client.request("DELETE", "/v1/assets", json={"asset_ids": list(ids), "reason": "user",
                                                       "remove_from_projects": yes}, headers=headers)


def _library(env, name: str) -> tuple:
    client, headers, *_ = env
    r = client.post("/v1/libraries", json={"name": name, "root_path": f"/tmp/{name}"}, headers=headers)
    assert r.status_code == 200, r.text
    return (client, headers, r.json()["library_id"], *env[3:])


def _settings(env, **body):
    client, headers, *_ = env
    return client.patch("/v1/tenant/settings", json=body, headers=headers)


@pytest.fixture(autouse=True)
def _back_to_30_days(env):
    yield
    _settings(env, trash_days=30, confirm_purge=True)


# ---------------------------------------------------------------------------
# Emptying deletes what was shown
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_emptying_one_librarys_trash_leaves_the_others(env):
    client, headers, *_ = env
    a_env, b_env = _library(env, "EmptyOneA"), _library(env, "EmptyOneB")
    a = _ingest(a_env, "x/a.mov", sha=_sha())
    a_other = _ingest(a_env, "y/a.mov", sha=_sha())
    b = _ingest(b_env, "x/b.mov", sha=_sha())
    _trash(env, a, a_other, b)

    r = client.request("DELETE", "/v1/trash/empty", json={"library_id": a_env[2], "path": "x"}, headers=headers)
    assert r.status_code == 200 and r.json()["deleted"] == 1, r.text
    assert not _exists(env, "assets", "asset_id", a)
    assert _reason(env, a_other) == "user" and _reason(env, b) == "user"


@pytest.mark.slow
def test_emptying_everything_leaves_a_trashed_librarys_clips_to_the_library(env):
    client, headers, *_ = env
    lib_env = _library(env, "EmptyKeepsLib")
    clip = _ingest(lib_env, "a.mov", sha=_sha())
    _trash(env, clip)
    assert client.delete(f"/v1/libraries/{lib_env[2]}", headers=headers).status_code == 204
    client.request("DELETE", "/v1/trash/empty", json={}, headers=headers)
    assert _reason(env, clip) == "user"
    # ...and the automatic purge leaves it to the library as well.
    _age(env, "assets", "deleted_at", "asset_id", clip, 400)
    with patch("src.server.search.quickwit_client.QuickwitClient", return_value=MagicMock()):
        client.post("/v1/upkeep", headers=headers)
    assert _reason(env, clip) == "user"


# ---------------------------------------------------------------------------
# The purge re-checks under a lock
# ---------------------------------------------------------------------------


def _waiting(fn, *args):
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(fn, *args)
    time.sleep(0.5)
    assert not future.done(), "didn't wait for the lock"
    pool.shutdown(wait=False)
    return future


@pytest.mark.slow
def test_a_library_restored_since_it_was_listed_is_never_deleted(env):
    from src.server.api.routers.libraries import delete_libraries_for_good
    from src.server.repository.tenant import LibraryRepository

    client, headers, *_ = env
    lib_env = _library(env, "PurgeRace")
    clip = _ingest(lib_env, "a.mov", sha=_sha())
    client.delete(f"/v1/libraries/{lib_env[2]}", headers=headers)
    _age(env, "libraries", "trashed_at", "library_id", lib_env[2], 40)
    cutoff = utcnow() - timedelta(days=30)
    with _db(env) as session:
        listed = [lib for lib in LibraryRepository(session).get_trashed() if lib.library_id == lib_env[2]]
        session.commit()
        assert client.post(f"/v1/libraries/{lib_env[2]}/restore", headers=headers).status_code == 200
        assert delete_libraries_for_good(session, env[4], listed, before=cutoff) == 0
    assert _exists(env, "libraries", "library_id", lib_env[2]) and _reason(env, clip) == "active"


@pytest.mark.slow
def test_restoring_a_library_waits_for_a_purge_in_progress(env):
    from src.server.repository.tenant import LibraryRepository

    client, headers, *_ = env
    lib_env = _library(env, "PurgeWait")
    client.delete(f"/v1/libraries/{lib_env[2]}", headers=headers)
    with _db(env) as session:
        assert LibraryRepository(session).lock_trashed(lib_env[2])
        waiting = _waiting(lambda: client.post(f"/v1/libraries/{lib_env[2]}/restore", headers=headers))
        LibraryRepository(session).hard_delete(lib_env[2])
    assert waiting.result(timeout=10).status_code == 404


@pytest.mark.slow
def test_a_project_restored_since_it_was_listed_is_never_deleted(env):
    from src.server.api.routers.projects import delete_projects_for_good
    from src.server.repository.tenant import ProjectRepository

    client, headers, *_ = env
    project_id = client.post("/v1/projects", json={"name": "Purge race project"}, headers=headers).json()["project_id"]
    client.delete(f"/v1/projects/{project_id}", headers=headers)
    _age(env, "projects", "deleted_at", "project_id", project_id, 40)
    cutoff = utcnow() - timedelta(days=30)
    with _db(env) as session:
        listed = ProjectRepository(session).list_trashed_before(cutoff)
        session.commit()
        assert project_id in {p.project_id for p in listed}
        assert client.post(f"/v1/projects/{project_id}/restore", json={}, headers=headers).status_code == 200
        delete_projects_for_good(session, listed, before=cutoff)
    assert _exists(env, "projects", "project_id", project_id)


@pytest.mark.slow
def test_a_clip_restored_and_archived_since_it_was_listed_is_never_deleted(env):
    from src.server.api.routers.trash import purge_assets
    from src.server.repository.tenant import AssetRepository

    client, headers, *_ = env
    clip = _ingest(env, "race/clip.mov", sha=_sha())
    _trash(env, clip)
    _age(env, "assets", "deleted_at", "asset_id", clip, 40)
    cutoff = utcnow() - timedelta(days=30)
    with _db(env) as session:
        listed = AssetRepository(session).list_trashed(asset_ids=[clip], trashed_before=cutoff)
        session.commit()
        assert client.post(f"/v1/assets/{clip}/restore", headers=headers).status_code == 204
        assert client.post("/v1/assets/archive", json={"asset_ids": [clip]}, headers=headers).status_code == 200
        assert purge_assets(session, env[4], listed, "", remove_from_projects=True, before=cutoff) == 0
    assert _reason(env, clip) == "archived"


@pytest.mark.slow
def test_a_clip_trashed_again_today_isnt_deleted_on_its_old_clock(env):
    from src.server.api.routers.trash import purge_assets
    from src.server.repository.tenant import AssetRepository

    client, headers, *_ = env
    clip = _ingest(env, "race/again.mov", sha=_sha())
    _trash(env, clip)
    _age(env, "assets", "deleted_at", "asset_id", clip, 40)
    cutoff = utcnow() - timedelta(days=30)
    with _db(env) as session:
        listed = AssetRepository(session).list_trashed(asset_ids=[clip], trashed_before=cutoff)
        session.commit()
        client.post(f"/v1/assets/{clip}/restore", headers=headers)
        _trash(env, clip)
        assert purge_assets(session, env[4], listed, "", remove_from_projects=True, before=cutoff) == 0
    assert _reason(env, clip) == "user"


# ---------------------------------------------------------------------------
# Restore puts a clip back where it was
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_restoring_a_clip_archived_before_it_was_trashed_puts_it_back_in_the_archive(env):
    client, headers, *_ = env
    shelved = _ingest(env, "back/shelved.mov", sha=_sha())
    gone = _ingest(env, "back/gone.mov", sha=_sha())
    live = _ingest(env, "back/live.mov", sha=_sha())
    client.post("/v1/assets/archive", json={"asset_ids": [shelved]}, headers=headers)
    client.request("DELETE", "/v1/assets", json={"asset_ids": [gone], "reason": "missing"}, headers=headers)
    _trash(env, shelved, gone, live)

    r = client.post("/v1/assets/restore", json={"asset_ids": [shelved, gone, live]}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["restored"] == [shelved, gone, live]
    assert r.json()["to_archive"] == [shelved, gone]
    assert (_reason(env, shelved), _reason(env, gone), _reason(env, live)) == ("archived", "missing", "active")

    _trash(env, shelved)
    assert client.post(f"/v1/assets/{shelved}/restore", headers=headers).status_code == 204
    assert _reason(env, shelved) == "archived"


# ---------------------------------------------------------------------------
# Trashing through an exclude filter asks about projects too
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_an_exclude_filter_trashing_clips_projects_use_asks_first(env):
    client, headers, library_id, *_ = env
    clip = _ingest(env, "Rejects/take1.mov", sha=_sha())
    client.post("/v1/projects", json={"name": "Uses a reject", "asset_ids": [clip]}, headers=headers)
    body = {"type": "exclude", "pattern": "Rejects/**", "trash_matching": True}
    r = client.post(f"/v1/libraries/{library_id}/filters", json=body, headers=headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "in_projects", r.text
    filters = client.get(f"/v1/libraries/{library_id}/filters", headers=headers).json()["excludes"]
    assert "Rejects/**" not in [f["pattern"] for f in filters]  # nothing happened
    assert _reason(env, clip) == "active"

    r = client.post(f"/v1/libraries/{library_id}/filters", json={**body, "remove_from_projects": True}, headers=headers)
    assert r.status_code == 201 and r.json()["trashed_count"] == 1, r.text
    assert _reason(env, clip) == "user"


# ---------------------------------------------------------------------------
# Shortening the trash days
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_shortening_the_trash_days_asks_when_it_would_delete_things_at_once(env):
    clip = _ingest(env, "short/clip.mov", sha=_sha())
    _trash(env, clip)
    _age(env, "assets", "deleted_at", "asset_id", clip, 10)

    assert _settings(env, trash_days=20).status_code == 200  # nothing that old: no question
    r = _settings(env, trash_days=7)
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "trash_days_shortened"
    assert err["details"]["clips"] >= 1 and set(err["details"]) == {"trash_days", "clips", "libraries", "projects"}
    assert _settings(env, trash_days=60).json()["trash_days"] == 60  # longer never asks
    r = _settings(env, trash_days=7, confirm_purge=True)
    assert r.status_code == 200 and r.json()["trash_days"] == 7


@pytest.mark.slow
def test_turning_the_trash_days_back_on_asks_too(env):
    clip = _ingest(env, "short/off.mov", sha=_sha())
    _trash(env, clip)
    _age(env, "assets", "deleted_at", "asset_id", clip, 100)
    assert _settings(env, trash_days=None, confirm_purge=True).status_code == 200
    assert _settings(env, trash_days=30).status_code == 409
    assert _settings(env, trash_days=None).status_code == 200  # off never asks


# ---------------------------------------------------------------------------
# Smaller things
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_trashing_asks_only_about_clips_that_would_go(env):
    """Already in the trash: nothing to ask about, nothing to do."""
    client, headers, *_ = env
    clip = _ingest(env, "ask/only.mov", sha=_sha())
    client.post("/v1/projects", json={"name": "Asks once", "asset_ids": [clip]}, headers=headers)
    assert _trash(env, clip).status_code == 200
    r = _trash(env, clip, yes=False)
    assert r.status_code == 200 and r.json()["trashed"] == [], r.text
    assert client.delete(f"/v1/assets/{clip}", headers=headers).status_code == 404


@pytest.mark.slow
def test_unarchive_never_brings_a_clip_back_into_a_trashed_library(env):
    client, headers, *_ = env
    lib_env = _library(env, "UnarchiveTrashedLib")
    clip = _ingest(lib_env, "a.mov", sha=_sha())
    client.post("/v1/assets/archive", json={"asset_ids": [clip]}, headers=headers)
    client.delete(f"/v1/libraries/{lib_env[2]}", headers=headers)
    r = client.post("/v1/assets/unarchive", json={"asset_ids": [clip]}, headers=headers)
    assert r.json() == {"unarchived": [], "skipped": [clip]}


@pytest.mark.slow
def test_a_public_page_never_shows_a_hidden_clips_picture(env):
    client, headers, library_id, *_ = env
    clip = _ingest(env, "pub/hidden.jpg", media_type="image", sha=_sha())
    assert client.patch(f"/v1/libraries/{library_id}", json={"is_public": True}, headers=headers).status_code == 200
    try:
        assert client.get(f"/v1/assets/{clip}/thumbnail", params={"public_library_id": library_id}).status_code == 200
        client.post("/v1/assets/archive", json={"asset_ids": [clip]}, headers=headers)
        r = client.get(f"/v1/assets/{clip}/thumbnail", params={"public_library_id": library_id})
        assert r.status_code == 404
    finally:
        client.patch(f"/v1/libraries/{library_id}", json={"is_public": False}, headers=headers)
