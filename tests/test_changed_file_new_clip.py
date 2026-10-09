# ruff: noqa: F811 — pytest fixtures are named as parameters
"""A different file at the same path is a new clip; the old one goes missing (Robert, Oct 9).

"A binary different file with the same name is a completely new file", and
"Old one goes missing": nothing made or written for the old file carries
over to the new one. The old clip is archived as missing, with everything
it had (its projects too), and comes back if its content does. A file
whose content isn't known (no hash) stays the clip it was.
"""

from __future__ import annotations

import pytest

from tests.test_analysis_proxy_api import _ingest, env  # noqa: F401 — the shared server fixture
from tests.test_archive_by_hand import _archive, _row, _sha, _unarchive

pytestmark = pytest.mark.slow


def _missing(env, *asset_ids: str) -> None:
    client, headers, *_ = env
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": list(asset_ids), "reason": "missing"},
                       headers=headers)
    assert r.status_code in (200, 204), r.text


def _note(env, asset_id: str, text: str) -> None:
    client, headers, *_ = env
    assert client.put(f"/v1/assets/{asset_id}/note", json={"text": text}, headers=headers).status_code in (200, 204)


def test_a_changed_file_is_a_new_clip_and_the_old_one_goes_missing(env):
    client, headers, *_ = env
    old = _ingest(env, "ncf/a.jpg", media_type="image", sha=_sha())
    _note(env, old, "the first take")
    r = client.post("/v1/projects", json={"name": "New clip", "asset_ids": [old]}, headers=headers)
    project_id = r.json()["project_id"]

    new = _ingest(env, "ncf/a.jpg", media_type="image", sha=_sha())
    assert new != old
    assert _row(env, old)[1] == "missing" and _row(env, old)[2] == "ncf/a.jpg"  # archived where it was
    assert _row(env, new)[0] is None
    detail = client.get(f"/v1/assets/{new}", headers=headers).json()
    assert detail["note"] is None  # nothing carries over
    project = client.get(f"/v1/projects/{project_id}", headers=headers).json()
    assert project["missing_asset_count"] == 1  # still in its project, as missing


def test_the_old_content_back_at_its_path_brings_its_clip_back(env):
    first_sha, second_sha = _sha(), _sha()
    first = _ingest(env, "ncf/back.jpg", media_type="image", sha=first_sha)
    second = _ingest(env, "ncf/back.jpg", media_type="image", sha=second_sha)
    again = _ingest(env, "ncf/back.jpg", media_type="image", sha=first_sha)
    assert again == first and _row(env, first)[0] is None
    assert _row(env, second)[1] == "missing"


def test_a_different_file_where_a_missing_clip_was_is_a_new_clip(env):
    gone = _ingest(env, "ncf/gone.jpg", media_type="image", sha=_sha())
    _missing(env, gone)
    new = _ingest(env, "ncf/gone.jpg", media_type="image", sha=_sha())
    assert new != gone
    assert _row(env, gone)[1] == "missing" and _row(env, new)[0] is None


def test_the_same_file_back_where_it_was_comes_back(env):
    sha = _sha()
    gone = _ingest(env, "ncf/same.jpg", media_type="image", sha=sha)
    _missing(env, gone)
    assert _ingest(env, "ncf/same.jpg", media_type="image", sha=sha) == gone
    assert _row(env, gone)[0] is None


def test_a_file_whose_content_isnt_known_stays_the_clip_it_was(env):
    clip = _ingest(env, "ncf/nohash.jpg", media_type="image")
    assert _ingest(env, "ncf/nohash.jpg", media_type="image") == clip
    known = _ingest(env, "ncf/nohash.jpg", media_type="image", sha=_sha())
    assert known == clip  # its first hash: the same file, now known


def test_unarchiving_onto_a_path_another_clip_holds_is_skipped(env):
    archived = _ingest(env, "ncf/held.jpg", media_type="image", sha=_sha())
    assert _archive(env, asset_ids=[archived]).status_code == 200
    holder = _ingest(env, "ncf/held.jpg", media_type="image", sha=_sha())  # another file there now
    assert holder != archived
    r = _unarchive(env, asset_ids=[archived])
    assert r.status_code == 200 and r.json()["skipped"] == [archived]
    assert _row(env, archived)[1] == "archived" and _row(env, holder)[0] is None


def test_restoring_from_the_trash_onto_a_path_another_clip_holds_is_skipped(env):
    client, headers, *_ = env
    trashed = _ingest(env, "ncf/binned.jpg", media_type="image", sha=_sha())
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [trashed], "reason": "user"}, headers=headers)
    assert r.status_code in (200, 204), r.text
    holder = _ingest(env, "ncf/binned.jpg", media_type="image", sha=_sha())
    r = client.post("/v1/assets/restore", json={"asset_ids": [trashed]}, headers=headers)
    assert r.status_code == 200 and r.json()["skipped"] == [trashed]
    assert _row(env, trashed)[1] == "user" and _row(env, holder)[0] is None
