# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Archive and trash, Robert's model (Oct 8).

Archive = out of sight, kept forever. A person archives one clip, a
selection, or every clip under a folder (the folder only picks the clips:
a file added later shows up as usual). A clip whose file went missing is
archived too, and comes back by itself when the file does.

Trash = the person wants it gone: restorable until it's deleted for good
after the account's trash days. Deleting an archived clip moves it to the
trash; nothing deletes an archived clip for good directly.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session

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


def _row(env, asset_id: str) -> tuple:
    with _db(env) as session:
        return session.execute(
            text("SELECT deleted_at, deleted_reason, rel_path FROM assets WHERE asset_id = :a"), {"a": asset_id}
        ).one()


def _visible(env, asset_id: str) -> bool:
    client, headers, *_ = env
    return client.get(f"/v1/assets/{asset_id}", headers=headers).status_code == 200


def _archive(env, **body):
    client, headers, *_ = env
    return client.post("/v1/assets/archive", json=body, headers=headers)


def _unarchive(env, **body):
    client, headers, *_ = env
    return client.post("/v1/assets/unarchive", json=body, headers=headers)


def _trash(env, *asset_ids: str, **extra):
    client, headers, *_ = env
    return client.request("DELETE", "/v1/assets", json={"asset_ids": list(asset_ids), "reason": "user", **extra},
                          headers=headers)


def _missing(env, *asset_ids: str) -> None:
    client, headers, *_ = env
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": list(asset_ids), "reason": "missing"},
                       headers=headers)
    assert r.status_code == 200, r.text


def _revision(env) -> int:
    client, headers, library_id, *_ = env
    return client.get(f"/v1/libraries/{library_id}/revision", headers=headers).json()["revision"]


# ---------------------------------------------------------------------------
# Archiving clips
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_an_archived_clip_leaves_sight_keeps_everything_and_comes_back(env):
    client, headers, *_ = env
    clip = _ingest(env, "hand/keep.mov", sha=_sha())
    assert client.put(f"/v1/assets/{clip}/note", json={"text": "the good take"}, headers=headers).status_code == 200

    r = _archive(env, asset_ids=[clip])
    assert r.status_code == 200, r.text
    assert r.json() == {"archived": [clip], "skipped": []}
    assert not _visible(env, clip)
    assert _row(env, clip)[1] == "archived"

    r = _unarchive(env, asset_ids=[clip])
    assert r.status_code == 200, r.text
    assert r.json() == {"unarchived": [clip], "skipped": []}
    assert _visible(env, clip)
    assert client.get(f"/v1/assets/{clip}", headers=headers).json()["note"] == "the good take"


@pytest.mark.slow
def test_only_clips_in_sight_can_be_archived(env):
    """Already archived, missing, trashed or unknown: skipped, and left as they were."""
    live = _ingest(env, "skip/live.mov", sha=_sha())
    gone = _ingest(env, "skip/gone.mov", sha=_sha())
    binned = _ingest(env, "skip/binned.mov", sha=_sha())
    _missing(env, gone)
    assert _trash(env, binned).status_code == 200

    r = _archive(env, asset_ids=[live, gone, binned, "ast_nope"])
    assert r.status_code == 200, r.text
    assert r.json() == {"archived": [live], "skipped": [gone, binned, "ast_nope"]}
    assert _row(env, gone)[1] == "missing"  # still comes back when its file does
    assert _row(env, binned)[1] == "user"


@pytest.mark.slow
def test_unarchive_brings_back_only_what_a_person_archived(env):
    """A missing file's clip comes back with its file, not with Unarchive."""
    gone = _ingest(env, "unarch/gone.mov", sha=_sha())
    _missing(env, gone)
    r = _unarchive(env, asset_ids=[gone])
    assert r.json() == {"unarchived": [], "skipped": [gone]}
    assert _row(env, gone)[1] == "missing"


@pytest.mark.slow
def test_archiving_a_folder_takes_everything_under_it_and_nothing_beside_it(env):
    inside = [_ingest(env, p, sha=_sha()) for p in ("Trips/Paris/a.mov", "Trips/Paris/day 2/b.mov")]
    beside = [_ingest(env, p, sha=_sha()) for p in ("Trips/Paris 2/c.mov", "Trips/Parisian.mov", "Trips/a.mov")]
    _, _, library_id, *_ = env

    r = _archive(env, library_id=library_id, path="Trips/Paris/")  # a trailing slash is the same folder
    assert r.status_code == 200, r.text
    assert sorted(r.json()["archived"]) == sorted(inside)
    assert not any(_visible(env, a) for a in inside)
    assert all(_visible(env, a) for a in beside)


@pytest.mark.slow
def test_a_folder_name_is_matched_literally(env):
    """_ and % in a folder's name are characters, not wildcards."""
    _, _, library_id, *_ = env
    literal = _ingest(env, "Lit/a_b%/x.mov", sha=_sha())
    lookalike = _ingest(env, "Lit/aXbYZ/y.mov", sha=_sha())
    r = _archive(env, library_id=library_id, path="Lit/a_b%")
    assert r.json()["archived"] == [literal]
    assert _visible(env, lookalike)


@pytest.mark.slow
def test_a_file_added_to_an_archived_folder_shows_up(env):
    """The folder isn't archived; its clips were."""
    _, _, library_id, *_ = env
    old = _ingest(env, "Later/old.mov", sha=_sha())
    _archive(env, library_id=library_id, path="Later")
    new = _ingest(env, "Later/new.mov", sha=_sha())
    assert _visible(env, new)
    assert not _visible(env, old)


@pytest.mark.slow
def test_unarchiving_a_folder_leaves_missing_files_archived(env):
    _, _, library_id, *_ = env
    mine = _ingest(env, "Shelf/mine.mov", sha=_sha())
    gone = _ingest(env, "Shelf/gone.mov", sha=_sha())
    _archive(env, library_id=library_id, path="Shelf")
    _missing(env, gone)  # no-op: already archived, so it stays the person's archive
    gone2 = _ingest(env, "Shelf/sub/gone2.mov", sha=_sha())
    _missing(env, gone2)

    r = _unarchive(env, library_id=library_id, path="Shelf")
    assert r.status_code == 200, r.text
    assert sorted(r.json()["unarchived"]) == sorted([mine, gone])
    assert _row(env, gone2)[1] == "missing"


@pytest.mark.slow
def test_a_folder_needs_its_library_and_a_selection_needs_ids(env):
    _, _, library_id, *_ = env
    assert _archive(env).status_code == 422
    assert _archive(env, path="Trips").status_code == 422
    assert _archive(env, asset_ids=["ast_x"], library_id=library_id, path="Trips").status_code == 422
    assert _archive(env, library_id="lib_nope", path="Trips").status_code == 404


@pytest.mark.slow
def test_the_whole_library_is_a_folder_too(env):
    """An empty path archives everything in the library; checked through a
    library of its own so the shared one stays in sight."""
    client, headers, *_ = env
    r = client.post("/v1/libraries", json={"name": "WholeLib", "root_path": "/tmp/whole-lib"}, headers=headers)
    assert r.status_code == 200, r.text
    lib = r.json()["library_id"]
    lib_env = (client, headers, lib, *env[3:])
    clips = [_ingest(lib_env, p, sha=_sha()) for p in ("a.mov", "deep/b.mov")]
    r = _archive(env, library_id=lib, path="")
    assert sorted(r.json()["archived"]) == sorted(clips)


@pytest.mark.slow
def test_archiving_and_unarchiving_refresh_open_grids(env):
    clip = _ingest(env, "rev/clip.mov", sha=_sha())
    before = _revision(env)
    _archive(env, asset_ids=[clip])
    after_archive = _revision(env)
    assert after_archive > before
    _unarchive(env, asset_ids=[clip])
    assert _revision(env) > after_archive


@pytest.mark.slow
def test_a_scan_never_undoes_an_archive(env):
    """The file is still on disk: scanners skip it, and ingest refuses it. A
    different file put at its path is a new clip (Robert, Oct 9)."""
    client, headers, library_id, *_ = env
    sha = _sha()
    clip = _ingest(env, "scan/kept.mov", sha=sha)
    _archive(env, asset_ids=[clip])

    items = client.get(f"/v1/libraries/{library_id}/ignored-paths", params={"limit": 1000}, headers=headers).json()
    item = next(i for i in items["items"] if i["rel_path"] == "scan/kept.mov")
    assert item["reason"] == "archived" and [f["sha256"] for f in item["files"]] == [sha]
    with pytest.raises(AssertionError, match="409"):
        _ingest(env, "scan/kept.mov", sha=sha)
    assert _row(env, clip)[1] == "archived"
    other = _ingest(env, "scan/kept.mov", sha=_sha())
    assert other != clip and _row(env, clip)[1] == "archived"


@pytest.mark.slow
def test_an_archived_clip_whose_file_goes_missing_stays_archived_by_hand(env):
    clip = _ingest(env, "gone/hand.mov", sha=_sha())
    _archive(env, asset_ids=[clip])
    _missing(env, clip)
    assert _row(env, clip)[1] == "archived"


@pytest.mark.slow
def test_its_content_turning_up_elsewhere_doesnt_unarchive_it(env):
    """Restore by content is for missing files, not a person's archive."""
    sha = _sha()
    clip = _ingest(env, "content/a.mov", sha=sha)
    _archive(env, asset_ids=[clip])
    other = _ingest(env, "content/b.mov", sha=sha)
    assert other != clip
    assert _row(env, clip)[1] == "archived"


# ---------------------------------------------------------------------------
# Who may archive
# ---------------------------------------------------------------------------


@pytest.fixture
def viewer_headers(env):
    import hashlib

    from sqlmodel import text as sql_text

    from src.server.database import get_control_session

    client, headers, *_ = env
    r = client.post("/v1/keys", json={"label": "viewer-archive"}, headers=headers)
    assert r.status_code == 200, r.text
    plaintext = r.json()["plaintext"]
    with get_control_session() as session:
        session.exec(sql_text("UPDATE api_keys SET role = 'viewer' WHERE key_hash = :h"),
                     params={"h": hashlib.sha256(plaintext.encode()).hexdigest()})
        session.commit()
    return {"Authorization": f"Bearer {plaintext}"}


@pytest.mark.slow
def test_archiving_needs_an_editor(env, viewer_headers):
    client, _headers, library_id, *_ = env
    clip = _ingest(env, "role/clip.mov", sha=_sha())
    for path, body in (("/v1/assets/archive", {"asset_ids": [clip]}),
                       ("/v1/assets/unarchive", {"asset_ids": [clip]}),
                       ("/v1/assets/archive", {"library_id": library_id, "path": "role"}),
                       ("/v1/assets/restore", {"asset_ids": [clip]})):
        assert client.post(path, json=body, headers=viewer_headers).status_code == 403, path
    assert _visible(env, clip)


# ---------------------------------------------------------------------------
# From archive to trash, and back out of the trash
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_trashing_an_archived_clip_starts_its_trash_clock_then(env):
    for archive in (lambda c: _archive(env, asset_ids=[c]), lambda c: _missing(env, c)):
        clip = _ingest(env, f"clock/{_sha()[:8]}.mov", sha=_sha())
        archive(clip)
        archived_at = _row(env, clip)[0]
        time.sleep(0.05)
        r = _trash(env, clip)
        assert r.status_code == 200, r.text
        assert r.json()["trashed"] == [clip]
        trashed_at, reason, _ = _row(env, clip)
        assert reason == "user"
        assert trashed_at > archived_at


@pytest.mark.slow
def test_a_person_trashing_never_takes_a_clip_from_its_trashed_library(env):
    """Trashed with its library, a clip comes back with the library."""
    client, headers, *_ = env
    r = client.post("/v1/libraries", json={"name": "LibKeepsReason", "root_path": "/tmp/lib-keeps"}, headers=headers)
    lib = r.json()["library_id"]
    clip = _ingest((client, headers, lib, *env[3:]), "a.mov", sha=_sha())
    assert client.request("DELETE", f"/v1/libraries/{lib}", json={}, headers=headers).status_code == 204
    assert _trash(env, clip).json()["trashed"] == []
    assert _row(env, clip)[1] == "library"


@pytest.mark.slow
def test_a_deletion_without_a_reason_is_recorded_as_missing(env):
    """What scanners sent before reasons existed (the Mac app still does)."""
    client, headers, *_ = env
    clip = _ingest(env, "noreason/a.mov", sha=_sha())
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [clip]}, headers=headers)
    assert r.status_code == 200, r.text
    assert _row(env, clip)[1] == "missing"


@pytest.mark.slow
def test_restore_takes_clips_out_of_the_trash_and_nothing_else(env):
    client, headers, *_ = env
    binned = _ingest(env, "restore/binned.mov", sha=_sha())
    shelved = _ingest(env, "restore/shelved.mov", sha=_sha())
    gone = _ingest(env, "restore/gone.mov", sha=_sha())
    _trash(env, binned)
    _archive(env, asset_ids=[shelved])
    _missing(env, gone)

    assert client.post(f"/v1/assets/{binned}/restore", headers=headers).status_code == 204
    assert _visible(env, binned)
    for clip, code in ((shelved, "archived"), (gone, "file_missing")):
        r = client.post(f"/v1/assets/{clip}/restore", headers=headers)
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == code
        assert not _visible(env, clip)


@pytest.mark.slow
def test_a_clip_trashed_with_its_library_comes_back_with_the_library(env):
    client, headers, *_ = env
    r = client.post("/v1/libraries", json={"name": "LibRestoreClip", "root_path": "/tmp/lib-rc"}, headers=headers)
    lib = r.json()["library_id"]
    clip = _ingest((client, headers, lib, *env[3:]), "a.mov", sha=_sha())
    client.request("DELETE", f"/v1/libraries/{lib}", json={}, headers=headers)
    r = client.post(f"/v1/assets/{clip}/restore", headers=headers)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "library_trashed"


@pytest.mark.slow
def test_restoring_a_selection(env):
    client, headers, *_ = env
    a = _ingest(env, "batchrestore/a.mov", sha=_sha())
    b = _ingest(env, "batchrestore/b.mov", sha=_sha())
    shelved = _ingest(env, "batchrestore/c.mov", sha=_sha())
    _trash(env, a, b)
    _archive(env, asset_ids=[shelved])
    before = _revision(env)
    r = client.post("/v1/assets/restore", json={"asset_ids": [a, b, shelved]}, headers=headers)
    assert r.status_code == 200, r.text
    assert sorted(r.json()["restored"]) == sorted([a, b])
    assert r.json()["skipped"] == [shelved]
    assert _visible(env, a) and _visible(env, b) and not _visible(env, shelved)
    assert _revision(env) > before


@pytest.mark.slow
def test_trash_and_restore_refresh_open_grids(env):
    client, headers, *_ = env
    clip = _ingest(env, "rev2/clip.mov", sha=_sha())
    before = _revision(env)
    _trash(env, clip)
    after_trash = _revision(env)
    assert after_trash > before
    client.post(f"/v1/assets/{clip}/restore", headers=headers)
    assert _revision(env) > after_trash


# ---------------------------------------------------------------------------
# Projects tell archived clips apart
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_project_counts_archived_clips_apart_from_missing_ones(env):
    client, headers, *_ = env
    shelved = _ingest(env, "proj/shelved.mov", sha=_sha())
    gone = _ingest(env, "proj/gone.mov", sha=_sha())
    binned = _ingest(env, "proj/binned.mov", sha=_sha())
    r = client.post("/v1/projects", json={"name": "Counts archive", "asset_ids": [shelved, gone, binned]},
                    headers=headers)
    assert r.status_code in (200, 201), r.text
    project_id = r.json()["project_id"]
    _archive(env, asset_ids=[shelved])
    _missing(env, gone)
    assert _trash(env, binned, remove_from_projects=True).status_code == 200

    item = client.get(f"/v1/projects/{project_id}", headers=headers).json()
    assert (item["archived_asset_count"], item["missing_asset_count"], item["trashed_asset_count"]) == (1, 1, 1)


# ---------------------------------------------------------------------------
# Trashing what projects use asks first: the trash deletes for good by itself
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_trashing_clips_that_projects_use_asks_first(env):
    client, headers, *_ = env
    clip = _ingest(env, "askproj/a.mov", sha=_sha())
    assert client.post("/v1/projects", json={"name": "Uses askproj", "asset_ids": [clip]},
                       headers=headers).status_code == 201
    r = _trash(env, clip)
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "in_projects"
    assert err["details"]["projects"][0]["name"] == "Uses askproj"
    assert client.delete(f"/v1/assets/{clip}", headers=headers).status_code == 409
    assert _visible(env, clip)  # nothing happened

    assert _trash(env, clip, remove_from_projects=True).json()["trashed"] == [clip]


@pytest.mark.slow
def test_a_scan_marking_files_missing_never_asks(env):
    """Archiving isn't deleting: a project keeps the clip, hidden until its file is back."""
    client, headers, *_ = env
    clip = _ingest(env, "askproj/b.mov", sha=_sha())
    client.post("/v1/projects", json={"name": "Uses askproj b", "asset_ids": [clip]}, headers=headers)
    _missing(env, clip)
    assert _archive(env, asset_ids=[_ingest(env, "askproj/c.mov", sha=_sha())]).status_code == 200


@pytest.mark.slow
def test_emptying_chosen_libraries_from_the_trash(env):
    client, headers, *_ = env
    libs = []
    for name in ("EmptyChosenA", "EmptyChosenB"):
        r = client.post("/v1/libraries", json={"name": name, "root_path": f"/tmp/{name}"}, headers=headers)
        libs.append(r.json()["library_id"])
        assert client.delete(f"/v1/libraries/{libs[-1]}", headers=headers).status_code == 204
    r = client.post("/v1/libraries/empty-trash", json={"library_ids": [libs[0]]}, headers=headers)
    assert r.status_code == 200 and r.json()["deleted"] == 1, r.text
    assert client.get(f"/v1/libraries/{libs[0]}", headers=headers).status_code == 404
    assert client.post(f"/v1/libraries/{libs[1]}/restore", headers=headers).status_code == 200
