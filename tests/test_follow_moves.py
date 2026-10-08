# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Follow moves and renames: an account-wide setting, on by default (Robert's call, Oct 8).

On, the same content is the same asset: a renamed or moved file keeps its
asset (the scanner's move detection, and restore by content on ingest), and
when a file goes missing while an empty, newer copy of it is in the library
(copy, then delete the original), the asset moves to the copy with its
notes, ratings, projects and people. Off, the path is the only identity:
a file at a new path is a new asset, and nothing is matched by content.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session

from src.server.models.tenant import Face, FacePersonMatch, Person
from tests.test_analysis_proxy_api import _ingest, env  # noqa: F401 — the shared server fixture


def _sha() -> str:
    return os.urandom(32).hex()


def _settings(env, **body):
    client, headers, *_ = env
    r = client.patch("/v1/tenant/settings", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(autouse=True)
def _moves_back_on(env):
    yield
    _settings(env, follow_moves=True)


def _archive(env, asset_id: str) -> None:
    client, headers, *_ = env
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [asset_id], "reason": "missing"}, headers=headers)
    assert r.status_code == 200, r.text


def _get(env, asset_id: str) -> dict:
    client, headers, *_ = env
    r = client.get(f"/v1/assets/{asset_id}", headers=headers)
    return r.json() if r.status_code == 200 else {"status_code": r.status_code}


def _stars(env, asset_id: str) -> int | None:
    client, headers, *_ = env
    ratings = client.post("/v1/assets/ratings/lookup", json={"asset_ids": [asset_id]}, headers=headers).json()["ratings"]
    return (ratings.get(asset_id) or {}).get("stars")


@contextmanager
def _db(env):
    engine = create_engine(env[-1])
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_moves_are_followed_until_an_admin_turns_them_off(env):
    client, headers, *_ = env
    assert client.get("/v1/tenant/settings", headers=headers).json()["follow_moves"] is True
    assert _settings(env, follow_moves=False)["follow_moves"] is False
    assert client.get("/v1/tenant/settings", headers=headers).json()["follow_moves"] is False
    assert _settings(env)["follow_moves"] is False  # left out: unchanged
    assert _settings(env, follow_moves=True)["follow_moves"] is True


@pytest.mark.slow
@pytest.mark.parametrize("bad", [None, "yes", 1])
def test_follow_moves_takes_true_or_false(env, bad):
    client, headers, *_ = env
    r = client.patch("/v1/tenant/settings", json={"follow_moves": bad}, headers=headers)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Off: the path is the only identity
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_off_a_file_at_a_new_path_is_a_new_asset(env):
    sha = _sha()
    asset = _ingest(env, "off/A001.mov", sha=sha)
    _archive(env, asset)
    _settings(env, follow_moves=False)
    assert _ingest(env, "off/renamed A001.mov", sha=sha) != asset
    assert _get(env, asset) == {"status_code": 404}  # still archived


@pytest.mark.slow
def test_off_a_file_back_at_its_own_path_still_comes_back(env):
    sha = _sha()
    asset = _ingest(env, "off/B001.mov", sha=sha)
    _archive(env, asset)
    _settings(env, follow_moves=False)
    assert _ingest(env, "off/B001.mov", sha=sha) == asset


@pytest.mark.slow
def test_off_moves_are_refused(env):
    client, headers, *_ = env
    asset = _ingest(env, "off/C001.mov", sha=_sha())
    _settings(env, follow_moves=False)
    r = client.post("/v1/assets/batch-moves", json={"items": [{"asset_id": asset, "rel_path": "off/moved/C001.mov"}]},
                    headers=headers)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "moves_off"
    assert _get(env, asset)["rel_path"] == "off/C001.mov"


# ---------------------------------------------------------------------------
# On: copy, then delete the original
# ---------------------------------------------------------------------------


def _copy_then_lose_the_original(env, name: str, *, prepare_copy=None) -> tuple[str, str]:
    """The original (rated 4) is copied elsewhere, the copy is scanned in, then
    the original goes missing. Returns (original, copy) asset ids."""
    client, headers, *_ = env
    sha = _sha()
    original = _ingest(env, f"Ingest/{name}", sha=sha)
    assert client.put(f"/v1/assets/{original}/rating", json={"stars": 4}, headers=headers).status_code == 200
    copy = _ingest(env, f"Projects/X/{name}", sha=sha)
    assert copy != original
    if prepare_copy:
        prepare_copy(copy)
    _archive(env, original)
    return original, copy


@pytest.mark.slow
def test_on_the_original_moves_to_its_empty_copy(env):
    client, headers, _lib, storage, *_ = env
    copy_files: list[str] = []

    def note_files(copy: str) -> None:
        with _db(env) as session:
            copy_files.extend(session.execute(
                text("SELECT proxy_key, thumbnail_key FROM assets WHERE asset_id = :a"), {"a": copy}).one())
        assert all(k and storage.abs_path(k).exists() for k in copy_files)

    original, copy = _copy_then_lose_the_original(env, "D001.mov", prepare_copy=note_files)
    assert _get(env, original)["rel_path"] == "Projects/X/D001.mov"
    assert _stars(env, original) == 4
    assert _get(env, copy) == {"status_code": 404}
    with _db(env) as session:
        assert session.execute(text("SELECT count(*) FROM assets WHERE asset_id = :a"), {"a": copy}).scalar() == 0
    assert not any(k and storage.abs_path(k).exists() for k in copy_files)  # the copy's files went with it


@pytest.mark.slow
@pytest.mark.parametrize("own", ["note", "rating", "project", "person"])
def test_on_a_copy_with_human_data_of_its_own_keeps_it(env, own):
    client, headers, *_ = env

    def give(copy: str) -> None:
        if own == "note":
            assert client.put(f"/v1/assets/{copy}/note", json={"text": "B-roll"}, headers=headers).status_code == 200
        elif own == "rating":
            assert client.put(f"/v1/assets/{copy}/rating", json={"stars": 2}, headers=headers).status_code == 200
        elif own == "project":
            r = client.post("/v1/projects", json={"name": f"Uses copy {uuid.uuid4().hex[:6]}", "asset_ids": [copy]},
                            headers=headers)
            assert r.status_code == 201, r.text
        else:
            with _db(env) as session:
                face_id, person_id = f"face_{uuid.uuid4().hex}", f"per_{uuid.uuid4().hex}"
                session.add(Person(person_id=person_id, display_name="Alex"))
                session.add(Face(face_id=face_id, asset_id=copy))
                session.flush()
                session.add(FacePersonMatch(match_id=f"fpm_{uuid.uuid4().hex}", face_id=face_id,
                                            person_id=person_id, confirmed=True))
                session.commit()

    original, copy = _copy_then_lose_the_original(env, f"E-{own}.mov", prepare_copy=give)
    assert _get(env, copy)["rel_path"] == f"Projects/X/E-{own}.mov"
    assert _get(env, original) == {"status_code": 404}  # archived, as before


@pytest.mark.slow
def test_on_an_older_copy_isnt_taken_over(env):
    """Only a copy made after the original: deleting the newer of two copies
    isn't copy-then-delete."""
    client, headers, *_ = env
    sha = _sha()
    older = _ingest(env, "older/F001.mov", sha=sha)
    newer = _ingest(env, "newer/F001.mov", sha=sha)
    assert client.put(f"/v1/assets/{newer}/rating", json={"stars": 5}, headers=headers).status_code == 200
    _archive(env, newer)
    assert _get(env, older)["rel_path"] == "older/F001.mov"
    assert _get(env, newer) == {"status_code": 404}


@pytest.mark.slow
def test_on_of_several_empty_copies_the_first_made_takes_over(env):
    client, headers, *_ = env
    sha = _sha()
    original = _ingest(env, "multi/G001.mov", sha=sha)
    first = _ingest(env, "multi/a/G001.mov", sha=sha)
    second = _ingest(env, "multi/b/G001.mov", sha=sha)
    _archive(env, original)
    assert _get(env, original)["rel_path"] == "multi/a/G001.mov"
    assert _get(env, first) == {"status_code": 404}
    assert _get(env, second)["rel_path"] == "multi/b/G001.mov"


@pytest.mark.slow
def test_off_the_original_is_just_archived(env):
    _settings(env, follow_moves=False)
    original, copy = _copy_then_lose_the_original(env, "H001.mov")
    assert _get(env, original) == {"status_code": 404}
    assert _get(env, copy)["rel_path"] == "Projects/X/H001.mov"


@pytest.mark.slow
def test_a_person_s_trash_never_moves_to_a_copy(env):
    client, headers, *_ = env
    sha = _sha()
    original = _ingest(env, "trash/I001.mov", sha=sha)
    copy = _ingest(env, "trash/copy/I001.mov", sha=sha)
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [original], "reason": "user"}, headers=headers)
    assert r.status_code == 200
    assert _get(env, copy)["rel_path"] == "trash/copy/I001.mov"
    assert _get(env, original) == {"status_code": 404}
