# ruff: noqa: F811 — pytest fixtures are named as parameters
"""The trash empties itself (Robert's call, Oct 8).

What a person trashed (clips, libraries, projects) is deleted for good once
it's been in the trash longer than the account's trash days: 30 until an
admin changes it or turns it off. Archived clips are never touched. The
trash and archive views list what's there.
"""

from __future__ import annotations

import os
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


def _settings(env, **body) -> dict:
    """Change settings, saying yes to deleting what fewer trash days would
    (the question itself is tested in test_archive_trash_safety)."""
    client, headers, *_ = env
    if "trash_days" in body:
        body = {"confirm_purge": True, **body}
    r = client.patch("/v1/tenant/settings", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(autouse=True)
def _back_to_30_days(env):
    yield
    _settings(env, trash_days=30)


def _trash(env, *asset_ids: str) -> None:
    client, headers, *_ = env
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": list(asset_ids), "reason": "user",
                                                    "remove_from_projects": True}, headers=headers)
    assert r.status_code == 200, r.text


def _age(env, table: str, column: str, key: str, value: str, days: int) -> None:
    """Pretend it went in the trash (or archive) `days` ago."""
    with _db(env) as session:
        session.execute(text(f"UPDATE {table} SET {column} = :t WHERE {key} = :v"),
                        {"t": utcnow() - timedelta(days=days), "v": value})
        session.commit()


def _exists(env, table: str, key: str, value: str) -> bool:
    with _db(env) as session:
        return session.execute(text(f"SELECT 1 FROM {table} WHERE {key} = :v"), {"v": value}).first() is not None


def _upkeep(env) -> dict:
    client, headers, *_ = env
    with patch("src.server.search.quickwit_client.QuickwitClient", return_value=MagicMock()):
        r = client.post("/v1/upkeep", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["trash_purge"]


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_thirty_days_until_an_admin_changes_it_or_turns_it_off(env):
    client, headers, *_ = env
    assert client.get("/v1/tenant/settings", headers=headers).json()["trash_days"] == 30
    assert _settings(env, trash_days=7)["trash_days"] == 7
    assert _settings(env, trash_days=None)["trash_days"] is None
    assert _settings(env, follow_moves=True)["trash_days"] is None  # left out, left alone
    for bad in (0, -1, 3651, "30", 1.5):
        assert client.patch("/v1/tenant/settings", json={"trash_days": bad}, headers=headers).status_code == 422


# ---------------------------------------------------------------------------
# Deleted for good when the days are up
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_clip_is_deleted_for_good_when_its_days_are_up(env):
    old = _ingest(env, "days/old.mov", sha=_sha())
    fresh = _ingest(env, "days/fresh.mov", sha=_sha())
    _trash(env, old, fresh)
    _age(env, "assets", "deleted_at", "asset_id", old, 31)
    _age(env, "assets", "deleted_at", "asset_id", fresh, 29)

    assert _upkeep(env)["clips"] >= 1
    assert not _exists(env, "assets", "asset_id", old)
    assert _exists(env, "assets", "asset_id", fresh)


@pytest.mark.slow
def test_a_clip_in_projects_leaves_them_without_asking_again(env):
    """Trashing it asked; the trash deleting it on its own is what was agreed to."""
    client, headers, *_ = env
    clip = _ingest(env, "days/inproj.mov", sha=_sha())
    r = client.post("/v1/projects", json={"name": "Days project", "asset_ids": [clip]}, headers=headers)
    project_id = r.json()["project_id"]
    _trash(env, clip)
    _age(env, "assets", "deleted_at", "asset_id", clip, 40)
    _upkeep(env)
    assert not _exists(env, "assets", "asset_id", clip)
    assert client.get(f"/v1/projects/{project_id}", headers=headers).json()["trashed_asset_count"] == 0


@pytest.mark.slow
def test_archived_clips_are_never_deleted_however_old(env):
    client, headers, *_ = env
    shelved = _ingest(env, "days/shelved.mov", sha=_sha())
    gone = _ingest(env, "days/gone.mov", sha=_sha())
    assert client.post("/v1/assets/archive", json={"asset_ids": [shelved]}, headers=headers).status_code == 200
    client.request("DELETE", "/v1/assets", json={"asset_ids": [gone], "reason": "missing"}, headers=headers)
    for clip in (shelved, gone):
        _age(env, "assets", "deleted_at", "asset_id", clip, 4000)
    _upkeep(env)
    assert _exists(env, "assets", "asset_id", shelved) and _exists(env, "assets", "asset_id", gone)


@pytest.mark.slow
def test_more_days_or_off_keeps_it(env):
    clip = _ingest(env, "days/kept.mov", sha=_sha())
    _trash(env, clip)
    _age(env, "assets", "deleted_at", "asset_id", clip, 31)
    _settings(env, trash_days=60)
    _upkeep(env)
    assert _exists(env, "assets", "asset_id", clip)
    _settings(env, trash_days=None)
    _age(env, "assets", "deleted_at", "asset_id", clip, 4000)
    assert _upkeep(env) == {"clips": 0, "libraries": 0, "projects": 0}
    assert _exists(env, "assets", "asset_id", clip)


@pytest.mark.slow
def test_a_library_and_a_project_go_when_their_days_are_up(env):
    client, headers, *_ = env
    r = client.post("/v1/libraries", json={"name": "DaysLib", "root_path": "/tmp/days-lib"}, headers=headers)
    lib = r.json()["library_id"]
    clip = _ingest((client, headers, lib, *env[3:]), "a.mov", sha=_sha())
    assert client.delete(f"/v1/libraries/{lib}", headers=headers).status_code == 204
    project_id = client.post("/v1/projects", json={"name": "Days old project"}, headers=headers).json()["project_id"]
    assert client.delete(f"/v1/projects/{project_id}", headers=headers).status_code in (200, 204)

    _upkeep(env)  # not yet
    assert _exists(env, "libraries", "library_id", lib) and _exists(env, "projects", "project_id", project_id)

    _age(env, "libraries", "trashed_at", "library_id", lib, 31)
    _age(env, "projects", "deleted_at", "project_id", project_id, 31)
    result = _upkeep(env)
    assert result["libraries"] >= 1 and result["projects"] >= 1
    assert not _exists(env, "libraries", "library_id", lib)
    assert not _exists(env, "assets", "asset_id", clip)
    assert not _exists(env, "projects", "project_id", project_id)


@pytest.mark.slow
def test_a_restored_library_starts_over(env):
    """Its clock stops when it's restored: trashed again, it gets its full days."""
    client, headers, *_ = env
    r = client.post("/v1/libraries", json={"name": "DaysAgain", "root_path": "/tmp/days-again"}, headers=headers)
    lib = r.json()["library_id"]
    client.delete(f"/v1/libraries/{lib}", headers=headers)
    _age(env, "libraries", "trashed_at", "library_id", lib, 29)
    assert client.post(f"/v1/libraries/{lib}/restore", headers=headers).status_code == 200
    client.delete(f"/v1/libraries/{lib}", headers=headers)
    _upkeep(env)
    with _db(env) as session:
        trashed_at = session.execute(text("SELECT trashed_at FROM libraries WHERE library_id = :l"),
                                     {"l": lib}).scalar()
    assert trashed_at > utcnow() - timedelta(minutes=5)


# ---------------------------------------------------------------------------
# The trash and archive views
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_the_trash_lists_what_a_person_trashed_with_when_it_goes(env):
    client, headers, library_id, *_ = env
    clips = [_ingest(env, f"view/trash/{i}.mov", sha=_sha()) for i in range(3)]
    shelved = _ingest(env, "view/trash/shelved.mov", sha=_sha())
    _trash(env, *clips)
    client.post("/v1/assets/archive", json={"asset_ids": [shelved]}, headers=headers)

    r = client.get("/v1/trash", params={"library_id": library_id, "path": "view/trash", "limit": 2}, headers=headers)
    assert r.status_code == 200, r.text
    page = r.json()
    assert page["total"] == 3 and page["trash_days"] == 30
    assert len(page["items"]) == 2 and page["next_cursor"]
    item = page["items"][0]
    assert item["rel_path"].startswith("view/trash/") and item["media_type"] == "video"
    assert item["library_name"]
    from datetime import datetime

    assert datetime.fromisoformat(item["expires_at"]) - datetime.fromisoformat(item["trashed_at"]) == timedelta(days=30)

    rest = client.get("/v1/trash", params={"library_id": library_id, "path": "view/trash", "after": page["next_cursor"]},
                      headers=headers).json()
    seen = [i["asset_id"] for i in page["items"] + rest["items"]]
    assert sorted(seen) == sorted(clips) and rest["next_cursor"] is None

    _settings(env, trash_days=None)
    assert client.get("/v1/trash", params={"path": "view/trash"}, headers=headers).json()["items"][0]["expires_at"] is None


@pytest.mark.slow
def test_the_archive_lists_archived_clips_and_says_which_files_are_missing(env):
    client, headers, library_id, *_ = env
    shelved = _ingest(env, "view/arch/shelved.mov", sha=_sha())
    gone = _ingest(env, "view/arch/gone.mov", sha=_sha())
    binned = _ingest(env, "view/arch/binned.mov", sha=_sha())
    client.post("/v1/assets/archive", json={"asset_ids": [shelved]}, headers=headers)
    client.request("DELETE", "/v1/assets", json={"asset_ids": [gone], "reason": "missing"}, headers=headers)
    _trash(env, binned)

    items = client.get("/v1/archive", params={"path": "view/arch"}, headers=headers).json()["items"]
    assert {i["asset_id"]: i["file_missing"] for i in items} == {shelved: False, gone: True}
    by_hand = client.get("/v1/archive", params={"path": "view/arch", "kind": "by_hand"}, headers=headers).json()
    assert [i["asset_id"] for i in by_hand["items"]] == [shelved] and by_hand["total"] == 1
    assert client.get("/v1/archive", params={"after": "garbage!"}, headers=headers).status_code == 400


@pytest.mark.slow
def test_a_trashed_librarys_clips_arent_listed(env):
    """They come back or go with their library: the library is what's in the trash."""
    client, headers, *_ = env
    r = client.post("/v1/libraries", json={"name": "ViewLib", "root_path": "/tmp/view-lib"}, headers=headers)
    lib = r.json()["library_id"]
    lib_env = (client, headers, lib, *env[3:])
    binned = _ingest(lib_env, "binned.mov", sha=_sha())
    _trash(lib_env, binned)
    client.delete(f"/v1/libraries/{lib}", headers=headers)
    assert client.get("/v1/trash", params={"library_id": lib}, headers=headers).json()["total"] == 0


@pytest.mark.slow
def test_hidden_clips_show_their_thumbnails_to_people_signed_in(env):
    client, headers, *_ = env
    clip = _ingest(env, "view/thumb.mov", sha=_sha())
    _trash(env, clip)
    assert client.get(f"/v1/assets/{clip}/thumbnail", headers=headers).status_code == 200
    assert client.get(f"/v1/assets/{clip}/thumbnail").status_code in (401, 403, 404)
