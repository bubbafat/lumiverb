# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Admins delete clips whose files went missing, for good (Robert, Oct 9).

The scenario: hundreds of licensed clips taken off the storage, archived as
missing and never coming back. An admin deletes them, in a library or under
a folder; the API asks first with the count, and about projects that use
them. Nothing else goes: clips archived by hand, the trash, clips still there.
If a file comes back afterwards, it's a new clip.
"""

from __future__ import annotations

import pytest

from tests.test_analysis_proxy_api import _ingest, env  # noqa: F401 — the shared server fixture
from tests.test_archive_by_hand import _archive, _db, _row, _sha
from tests.test_archive_trash_safety import _key_with_role

pytestmark = pytest.mark.slow


def _missing(env, *asset_ids: str) -> None:
    client, headers, *_ = env
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": list(asset_ids), "reason": "missing"},
                       headers=headers)
    assert r.status_code in (200, 204), r.text


def _delete_missing(env, headers=None, **body):
    client, own, *_ = env
    return client.request("DELETE", "/v1/archive/missing", json=body, headers=headers or own)


def _exists(env, asset_id: str) -> bool:
    from sqlalchemy import text

    with _db(env) as s:
        return s.execute(text("SELECT 1 FROM assets WHERE asset_id = :a"), {"a": asset_id}).first() is not None


def test_deleting_missing_clips_asks_first_with_the_count(env):
    gone = [_ingest(env, f"dm/gone{i}.jpg", media_type="image", sha=_sha()) for i in range(3)]
    kept = _ingest(env, "dm/still-there.jpg", media_type="image", sha=_sha())
    by_hand = _ingest(env, "dm/archived.jpg", media_type="image", sha=_sha())
    _missing(env, *gone)
    assert _archive(env, asset_ids=[by_hand]).status_code == 200
    client, headers, library_id, *_ = env

    r = _delete_missing(env, library_id=library_id)
    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert error["code"] == "confirm_delete_missing" and error["details"] == {"count": 3}
    assert all(_exists(env, a) for a in gone)  # nothing yet

    # A count that's no longer right asks again.
    assert _delete_missing(env, library_id=library_id, count=2).status_code == 409
    r = _delete_missing(env, library_id=library_id, count=3)
    assert r.status_code == 200 and r.json() == {"deleted": 3}
    assert not any(_exists(env, a) for a in gone)
    assert _exists(env, kept) and _row(env, kept)[0] is None
    assert _row(env, by_hand)[1] == "archived"  # archived by a person: not this
    assert _delete_missing(env, library_id=library_id).json() == {"deleted": 0}  # nothing left to ask about


def test_only_under_a_folder_when_asked(env):
    client, headers, library_id, *_ = env
    inside = _ingest(env, "dm-folder/licensed/a.jpg", media_type="image", sha=_sha())
    outside = _ingest(env, "dm-folder/own/b.jpg", media_type="image", sha=_sha())
    _missing(env, inside, outside)
    r = _delete_missing(env, library_id=library_id, path="dm-folder/licensed")
    assert r.json()["error"]["details"] == {"count": 1}
    assert _delete_missing(env, library_id=library_id, path="dm-folder/licensed", count=1).json() == {"deleted": 1}
    assert not _exists(env, inside) and _exists(env, outside)


def test_clips_in_projects_ask_about_the_projects_too(env):
    client, headers, library_id, *_ = env
    clip = _ingest(env, "dm-project/a.jpg", media_type="image", sha=_sha())
    r = client.post("/v1/projects", json={"name": "Delete missing", "asset_ids": [clip]}, headers=headers)
    assert r.status_code in (200, 201), r.text
    _missing(env, clip)
    r = _delete_missing(env, library_id=library_id, path="dm-project", count=1)
    assert r.status_code == 409 and r.json()["error"]["code"] == "in_projects"
    assert _exists(env, clip)
    r = _delete_missing(env, library_id=library_id, path="dm-project", count=1, remove_from_projects=True)
    assert r.json() == {"deleted": 1}


def test_a_file_that_comes_back_after_is_a_new_clip(env):
    client, headers, library_id, *_ = env
    sha = _sha()
    clip = _ingest(env, "dm-back/a.jpg", media_type="image", sha=sha)
    _missing(env, clip)
    _delete_missing(env, library_id=library_id, path="dm-back", count=1)
    again = _ingest(env, "dm-back/a.jpg", media_type="image", sha=sha)  # not ignored: the file is welcome back
    assert again != clip and _row(env, again)[0] is None


def test_only_admins_delete_missing_clips(env):
    client, headers, library_id, *_ = env
    clip = _ingest(env, "dm-role/a.jpg", media_type="image", sha=_sha())
    _missing(env, clip)
    for role in ("editor", "viewer"):
        assert _delete_missing(env, headers=_key_with_role(env, role), library_id=library_id,
                               path="dm-role", count=1).status_code == 403
    assert _exists(env, clip)
