# ruff: noqa: F811 — pytest fixtures are named as parameters
"""The archive model: a missing file's asset is archived, and restored when its content turns up.

The scanner marks files it no longer finds "missing" (archived): the asset
keeps everything. A file that appears at a path the library doesn't know is
first matched by content (SHA-256) against the same library's archived
assets; a match is restored at the new path, ratings, projects, faces and
all. Users' trash is never undone this way, and other libraries aren't
searched (Robert's call, Oct 8).
"""

from __future__ import annotations

import os

import pytest

from tests.test_analysis_proxy_api import _ingest, env  # noqa: F401 — the shared server fixture


def _sha() -> str:
    return os.urandom(32).hex()


def _archive(env, asset_id: str, reason: str = "missing") -> None:
    client, headers, *_ = env
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [asset_id], "reason": reason}, headers=headers)
    assert r.status_code == 200, r.text


def _get(env, asset_id: str) -> dict:
    client, headers, *_ = env
    r = client.get(f"/v1/assets/{asset_id}", headers=headers)
    return r.json() if r.status_code == 200 else {"status_code": r.status_code}


@pytest.mark.slow
def test_a_moved_file_restores_its_archived_asset_with_everything(env):
    client, headers, library_id, *_ = env
    sha = _sha()
    old = _ingest(env, "Day 1/A001.mov", sha=sha)
    assert client.put(f"/v1/assets/{old}/rating", json={"stars": 4, "favorite": True}, headers=headers).status_code == 200
    r = client.post("/v1/projects", json={"name": "Keepers", "asset_ids": [old]}, headers=headers)
    assert r.status_code == 201, r.text
    project_id = r.json()["project_id"]
    _archive(env, old)
    assert _get(env, old) == {"status_code": 404}

    # The same content turns up elsewhere in the library, a scan later.
    new = _ingest(env, "Archive/2026/A001.mov", sha=sha)
    assert new == old
    detail = _get(env, old)
    assert detail["rel_path"] == "Archive/2026/A001.mov"
    ratings = client.post("/v1/assets/ratings/lookup", json={"asset_ids": [old]}, headers=headers).json()["ratings"]
    assert ratings[old]["stars"] == 4 and ratings[old]["favorite"] is True
    members = client.get(f"/v1/projects/{project_id}/assets", headers=headers).json()["items"]
    assert [m["asset_id"] for m in members] == [old]


@pytest.mark.slow
def test_a_users_trash_is_never_undone_by_content(env):
    sha = _sha()
    trashed = _ingest(env, "trash/B001.mov", sha=sha)
    _archive(env, trashed, reason="user")
    new = _ingest(env, "elsewhere/B001.mov", sha=sha)
    assert new != trashed


@pytest.mark.slow
def test_a_copy_of_a_file_still_there_is_a_new_asset(env):
    sha = _sha()
    original = _ingest(env, "copies/C001.mov", sha=sha)
    copy = _ingest(env, "copies/C001 copy.mov", sha=sha)
    assert copy != original
    assert _get(env, original)["rel_path"] == "copies/C001.mov"


@pytest.mark.slow
def test_only_the_same_librarys_archive_is_searched(env):
    client, headers, *_ = env
    sha = _sha()
    here = _ingest(env, "lib1/D001.mov", sha=sha)
    _archive(env, here)
    r = client.post("/v1/libraries", json={"name": "Other", "root_path": "/Volumes/media-02/Other"}, headers=headers)
    assert r.status_code == 200, r.text
    other_env = (client, headers, r.json()["library_id"], *env[3:])
    there = _ingest(other_env, "D001.mov", sha=sha)
    assert there != here
    assert _get(env, here) == {"status_code": 404}


@pytest.mark.slow
def test_of_several_archived_matches_the_latest_comes_back(env):
    sha = _sha()
    first = _ingest(env, "dupes/E001.mov", sha=sha)
    _archive(env, first)
    second = _ingest(env, "dupes/E001 again.mov", sha=sha)  # restores `first`...
    assert second == first
    _archive(env, first)
    # ...and with two archived copies of the content, the most recent wins.
    other = _ingest(env, "dupes2/E001.mov", sha=sha)
    assert other == first


@pytest.mark.slow
def test_a_file_back_at_its_own_path_still_comes_back(env):
    sha = _sha()
    asset = _ingest(env, "same/F001.mov", sha=sha)
    _archive(env, asset)
    assert _ingest(env, "same/F001.mov", sha=sha) == asset
    assert _get(env, asset)["rel_path"] == "same/F001.mov"


# ---------------------------------------------------------------------------
# Deleting a library that still holds archived clips: the user says what happens to them
# ---------------------------------------------------------------------------


def _library_with_archived(env, name: str, in_project: bool = False) -> tuple[str, str, tuple]:
    client, headers, *_ = env
    r = client.post("/v1/libraries", json={"name": name, "root_path": f"/Volumes/media-01/{name}"}, headers=headers)
    assert r.status_code == 200, r.text
    lib_env = (client, headers, r.json()["library_id"], *env[3:])
    archived = _ingest(lib_env, "gone.mov", sha=_sha())
    _ingest(lib_env, "here.mov", sha=_sha())
    if in_project:
        assert client.post("/v1/projects", json={"name": f"Uses {name}", "asset_ids": [archived]},
                           headers=headers).status_code == 201
    _archive(lib_env, archived)
    return lib_env[2], archived, lib_env


def _still_archived(env, asset_id: str) -> bool:
    """True if the asset is still there, archived: naming it for deletion finds it (then puts it back)."""
    client, headers, *_ = env
    r = client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [asset_id], "include_missing": True,
                                                          "remove_from_projects": True}, headers=headers)
    return r.json()["deleted"] == 1


@pytest.mark.slow
def test_deleting_a_library_with_archived_clips_asks_what_to_do(env):
    client, headers, *_ = env
    library_id, archived, _ = _library_with_archived(env, "AskFirst")
    r = client.delete(f"/v1/libraries/{library_id}", headers=headers)
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "archived_clips"
    assert err["details"] == {"archived_clips": 1}
    # Nothing happened yet.
    assert client.get(f"/v1/libraries/{library_id}", headers=headers).status_code == 200


@pytest.mark.slow
def test_keep_leaves_archived_clips_with_the_trashed_library(env):
    client, headers, *_ = env
    library_id, archived, _ = _library_with_archived(env, "KeepThem")
    r = client.request("DELETE", f"/v1/libraries/{library_id}", json={"archived": "keep"}, headers=headers)
    assert r.status_code == 204, r.text
    assert _still_archived(env, archived)


@pytest.mark.slow
def test_delete_removes_archived_clips_for_good_first(env):
    client, headers, *_ = env
    library_id, archived, _ = _library_with_archived(env, "DeleteThem")
    r = client.request("DELETE", f"/v1/libraries/{library_id}", json={"archived": "delete"}, headers=headers)
    assert r.status_code == 204, r.text
    assert not _still_archived(env, archived)


@pytest.mark.slow
def test_archived_clips_in_projects_need_the_projects_say_too(env):
    client, headers, *_ = env
    library_id, archived, _ = _library_with_archived(env, "InProjects", in_project=True)
    r = client.request("DELETE", f"/v1/libraries/{library_id}", json={"archived": "delete"}, headers=headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "in_projects"
    r = client.request("DELETE", f"/v1/libraries/{library_id}",
                       json={"archived": "delete", "remove_from_projects": True}, headers=headers)
    assert r.status_code == 204, r.text


@pytest.mark.slow
def test_a_library_without_archived_clips_deletes_as_before(env):
    client, headers, *_ = env
    r = client.post("/v1/libraries", json={"name": "Plain", "root_path": "/Volumes/media-01/Plain"}, headers=headers)
    library_id = r.json()["library_id"]
    _ingest((client, headers, library_id, *env[3:]), "here.mov", sha=_sha())
    assert client.delete(f"/v1/libraries/{library_id}", headers=headers).status_code == 204
