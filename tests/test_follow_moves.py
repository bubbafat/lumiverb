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
@pytest.mark.parametrize("own", ["note", "rating", "project", "person", "location", "removed_transcript"])
def test_on_a_copy_with_human_data_of_its_own_keeps_it(env, own):
    client, headers, *_ = env

    def give(copy: str) -> None:
        if own == "location":
            r = client.put("/v1/assets/locations", json={"asset_ids": [copy], "lat": 40.0, "lon": -74.0},
                           headers=headers)
            assert r.status_code == 200, r.text
        elif own == "removed_transcript":  # a person removed the machine's transcript for good
            from src.server.repository import lineage

            with _db(env) as session:
                lineage.record(session, copy, "transcript", None, person=True, outcome="empty")
        elif own == "note":
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


# ---------------------------------------------------------------------------
# What counts as a person's work on the copy (review of this branch)
# ---------------------------------------------------------------------------

SRT = "1\n00:00:00,000 --> 00:00:02,000\nHello there\n"


@pytest.mark.slow
@pytest.mark.parametrize("own", ["transcript", "rejection"])
def test_on_a_copy_with_a_person_s_transcript_or_rejection_keeps_it(env, own):
    client, headers, *_ = env

    def give(copy: str) -> None:
        if own == "transcript":
            r = client.post(f"/v1/assets/{copy}/transcript",
                            json={"srt": SRT, "language": "en", "source": "manual"}, headers=headers)
            assert r.status_code == 200, r.text
        else:
            from src.server.models.tenant import FacePersonRejection

            with _db(env) as session:
                face_id, person_id = f"face_{uuid.uuid4().hex}", f"per_{uuid.uuid4().hex}"
                session.add(Person(person_id=person_id, display_name="Not Alex"))
                session.add(Face(face_id=face_id, asset_id=copy))
                session.flush()
                session.add(FacePersonRejection(face_id=face_id, person_id=person_id))
                session.commit()

    original, copy = _copy_then_lose_the_original(env, f"J-{own}.mov", prepare_copy=give)
    assert _get(env, copy)["rel_path"] == f"Projects/X/J-{own}.mov"
    assert _get(env, original) == {"status_code": 404}


@pytest.mark.slow
def test_on_unconfirmed_faces_on_the_copy_go_with_it(env):
    face_id = f"face_{uuid.uuid4().hex}"

    def give(copy: str) -> None:
        with _db(env) as session:
            session.add(Face(face_id=face_id, asset_id=copy))
            session.commit()

    original, _ = _copy_then_lose_the_original(env, "K001.mov", prepare_copy=give)
    assert _get(env, original)["rel_path"] == "Projects/X/K001.mov"
    with _db(env) as session:
        assert session.execute(text("SELECT count(*) FROM faces WHERE face_id = :f"), {"f": face_id}).scalar() == 0


@pytest.mark.slow
def test_on_a_cover_set_to_the_copy_follows_the_asset(env):
    library_id = env[2]

    def give(copy: str) -> None:
        with _db(env) as session:
            session.execute(text("UPDATE libraries SET cover_asset_id = :a WHERE library_id = :l"),
                            {"a": copy, "l": library_id})
            session.commit()

    original, _ = _copy_then_lose_the_original(env, "L001.mov", prepare_copy=give)
    with _db(env) as session:
        cover = session.execute(text("SELECT cover_asset_id FROM libraries WHERE library_id = :l"),
                                {"l": library_id}).scalar()
        session.execute(text("UPDATE libraries SET cover_asset_id = NULL WHERE library_id = :l"), {"l": library_id})
        session.commit()
    assert cover == original


@pytest.mark.slow
def test_on_the_moved_asset_takes_the_copy_s_file_stat_and_goes_back_in_search(env):
    client, headers, library_id, *_ = env
    stat: dict = {}

    def note_stat(copy: str) -> None:
        with _db(env) as session:
            stat.update(session.execute(text("SELECT file_size, file_mtime FROM assets WHERE asset_id = :a"),
                                        {"a": copy}).mappings().one())
        # Archiving doesn't bump the revision; the handover must, or open grids keep the copy's tile.
        stat["revision"] = client.get(f"/v1/libraries/{library_id}/revision", headers=headers).json()["revision"]

    original, _ = _copy_then_lose_the_original(env, "M001.mov", prepare_copy=note_stat)
    with _db(env) as session:
        row = session.execute(text("SELECT file_size, file_mtime, search_synced_at FROM assets WHERE asset_id = :a"),
                              {"a": original}).mappings().one()
    assert (row["file_size"], row["file_mtime"]) == (stat["file_size"], stat["file_mtime"])
    assert row["search_synced_at"] is None
    assert client.get(f"/v1/libraries/{library_id}/revision", headers=headers).json()["revision"] > stat["revision"]


@pytest.mark.slow
def test_the_archive_response_names_what_moved_to_a_copy(env):
    client, headers, *_ = env
    sha = _sha()
    original = _ingest(env, "resp/N001.mov", sha=sha)
    _ingest(env, "resp/copy/N001.mov", sha=sha)
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [original], "reason": "missing"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["handed_over"] == [original]


@pytest.mark.slow
def test_an_original_without_a_hash_is_just_archived(env):
    original = _ingest(env, "nohash/O001.mov")
    copy = _ingest(env, "nohash/copy/O001.mov")
    _archive(env, original)
    assert _get(env, original) == {"status_code": 404}
    assert _get(env, copy)["rel_path"] == "nohash/copy/O001.mov"


@pytest.mark.slow
def test_a_handover_that_fails_leaves_the_archive_done(env):
    """The archive committed before the handover: a failure there is logged,
    not a failed request (the scan would stop and never retry)."""
    from unittest.mock import patch

    sha = _sha()
    original = _ingest(env, "fail/P001.mov", sha=sha)
    copy = _ingest(env, "fail/copy/P001.mov", sha=sha)
    with patch("src.server.api.routers.trash.purge_assets", side_effect=RuntimeError("boom")):
        _archive(env, original)
    assert _get(env, original) == {"status_code": 404}
    assert _get(env, copy)["rel_path"] == "fail/copy/P001.mov"


@pytest.mark.slow
def test_turning_moves_on_and_off_keeps_the_playback_cuts(env):
    from unittest.mock import patch

    with patch("src.server.api.routers.playback.clear_cuts") as clear:
        _settings(env, follow_moves=False)
        _settings(env, follow_moves=True)
    clear.assert_not_called()


@pytest.mark.slow
def test_active_assets_are_indexed_by_content(env):
    with _db(env) as session:
        index = session.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_assets_active_sha'")).scalar()
    assert index is not None and "deleted_at IS NULL" in index


# ---------------------------------------------------------------------------
# A person working on the copy while it's handed over: nothing is lost silently
# ---------------------------------------------------------------------------


def _face_and_person(env, copy: str) -> tuple[str, str]:
    face_id, person_id = f"face_{uuid.uuid4().hex}", f"per_{uuid.uuid4().hex}"
    with _db(env) as session:
        session.add(Person(person_id=person_id, display_name="Alex"))
        session.add(Face(face_id=face_id, asset_id=copy))
        session.commit()
    return face_id, person_id


def _confirm(conn, face_id: str, person_id: str) -> None:
    conn.execute(text("INSERT INTO face_person_matches (match_id, face_id, person_id, confirmed, created_at)"
                      " VALUES (:m, :f, :p, true, now())"), {"m": f"fpm_{uuid.uuid4().hex}", "f": face_id, "p": person_id})


@pytest.mark.slow
def test_a_person_confirmed_on_the_copy_just_before_the_lock_keeps_it(env, monkeypatch):
    from src.server.repository.tenant import AssetRepository

    sha = _sha()
    original = _ingest(env, "race/Q001.mov", sha=sha)
    copy = _ingest(env, "race/copy/Q001.mov", sha=sha)
    face_id, person_id = _face_and_person(env, copy)
    real_lock = AssetRepository.lock_copy
    engine = create_engine(env[-1])

    def confirm_then_lock(self, asset_id):
        if asset_id == copy:
            with engine.connect() as conn:
                _confirm(conn, face_id, person_id)
                conn.commit()
        return real_lock(self, asset_id)

    monkeypatch.setattr(AssetRepository, "lock_copy", confirm_then_lock)
    _archive(env, original)
    engine.dispose()
    assert _get(env, copy)["rel_path"] == "race/copy/Q001.mov"
    with _db(env) as session:
        assert session.execute(text("SELECT count(*) FROM face_person_matches WHERE face_id = :f"),
                               {"f": face_id}).scalar() == 1


@pytest.mark.slow
def test_a_rating_in_flight_on_the_copy_keeps_it(env, monkeypatch):
    """A rating whose transaction is open when the handover goes to lock the copy."""
    import threading
    import time

    from src.server.repository.tenant import AssetRepository

    sha = _sha()
    original = _ingest(env, "race/R001.mov", sha=sha)
    copy = _ingest(env, "race/copy/R001.mov", sha=sha)
    real_lock = AssetRepository.lock_copy
    engine = create_engine(env[-1])

    def rating_in_flight_then_lock(self, asset_id):
        if asset_id == copy:
            conn = engine.connect()
            conn.execute(text("INSERT INTO asset_ratings (user_id, asset_id, favorite, stars, updated_at)"
                              " VALUES ('usr_x', :a, false, 5, now())"), {"a": copy})

            def commit_later() -> None:
                time.sleep(0.5)
                conn.commit()
                conn.close()

            committer = threading.Thread(target=commit_later)
            committer.start()
            threads.append(committer)
        return real_lock(self, asset_id)  # waits for the rating, or gives way

    threads: list = []
    monkeypatch.setattr(AssetRepository, "lock_copy", rating_in_flight_then_lock)
    _archive(env, original)
    for thread in threads:
        thread.join(10)
    engine.dispose()
    assert _get(env, copy)["rel_path"] == "race/copy/R001.mov"
    with _db(env) as session:  # usr_x's rating: the lookup only shows the caller's own
        assert session.execute(text("SELECT stars FROM asset_ratings WHERE asset_id = :a"), {"a": copy}).scalar() == 5


@pytest.mark.slow
def test_a_person_confirmed_on_the_copy_during_the_handover_isnt_lost_silently(env, monkeypatch):
    """Once the copy and its faces are locked, a confirmation waits for the
    handover, then fails (its face is gone): it never commits and vanishes."""
    import threading

    from src.server.repository.tenant import AssetRepository

    sha = _sha()
    original = _ingest(env, "race/S001.mov", sha=sha)
    copy = _ingest(env, "race/copy/S001.mov", sha=sha)
    face_id, person_id = _face_and_person(env, copy)
    real_lock = AssetRepository.lock_copy
    engine = create_engine(env[-1])
    outcome: dict = {}

    def confirm() -> None:
        try:
            with engine.connect() as conn:
                conn.execute(text("SET lock_timeout = '10s'"))
                _confirm(conn, face_id, person_id)
                conn.commit()
                outcome["committed"] = True
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = type(exc).__name__

    def lock_then_confirm(self, asset_id):
        locked = real_lock(self, asset_id)
        if asset_id == copy:
            thread = threading.Thread(target=confirm)
            thread.start()
            thread.join(0.5)
            outcome["waited"] = thread.is_alive()
            outcome["thread"] = thread
        return locked

    monkeypatch.setattr(AssetRepository, "lock_copy", lock_then_confirm)
    _archive(env, original)
    assert "thread" in outcome, "the handover never locked the copy"
    outcome["thread"].join(15)
    assert outcome["waited"], "the confirmation didn't wait for the handover"
    engine.dispose()
    with _db(env) as session:
        matches = session.execute(text("SELECT count(*) FROM face_person_matches WHERE face_id = :f"),
                                  {"f": face_id}).scalar()
    assert not outcome.get("committed") or matches == 1, outcome
    assert "error" in outcome or matches == 1, outcome



@pytest.mark.slow
@pytest.mark.parametrize("action", ["unassign", "merge"])
def test_a_person_s_action_on_the_copy_during_the_handover_wins(env, monkeypatch, action):
    """Un-assigning a face or merging people locks matches or people first, then
    faces: the opposite order to the handover. The handover gives way (it times
    out and the original stays archived) rather than deadlocking the person's
    request into an error."""
    import threading
    import time

    from src.server.repository.tenant import AssetRepository, PersonRepository

    sha = _sha()
    original = _ingest(env, f"lockorder/{action}/A.mov", sha=sha)
    copy = _ingest(env, f"lockorder/{action}/copy/A.mov", sha=sha)
    face_id, s_id, t_id = f"face_{uuid.uuid4().hex}", f"per_{uuid.uuid4().hex}", f"per_{uuid.uuid4().hex}"
    with _db(env) as session:
        session.add(Person(person_id=s_id, display_name="S"))
        session.add(Person(person_id=t_id, display_name="T"))
        session.flush()
        session.add(Face(face_id=face_id, asset_id=copy, person_id=s_id if action == "merge" else None))
        session.flush()
        session.add(FacePersonMatch(match_id=f"fpm_{uuid.uuid4().hex}", face_id=face_id, person_id=s_id,
                                    confidence=0.8, confirmed=False))
        if action == "merge":
            session.execute(text("UPDATE people SET representative_face_id = :f WHERE person_id = :p"),
                            {"f": face_id, "p": s_id})
        session.commit()
    real_check = AssetRepository.has_human_data
    engine = create_engine(env[-1])
    outcome: dict = {}

    def act() -> None:
        try:
            with Session(engine) as session:
                people = PersonRepository(session)
                outcome["ok"] = (people.merge(t_id, s_id) is not None if action == "merge"
                                 else people.unassign_face(face_id))
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"

    def check_then_act(self, asset_id):
        result = real_check(self, asset_id)
        if asset_id == copy:
            thread = threading.Thread(target=act)
            thread.start()
            time.sleep(0.5)
            outcome["thread"] = thread
        return result

    monkeypatch.setattr(AssetRepository, "has_human_data", check_then_act)
    _archive(env, original)
    outcome["thread"].join(15)
    engine.dispose()
    assert "error" not in outcome, outcome
    assert outcome.get("ok"), outcome


@pytest.mark.slow
def test_a_failure_after_the_move_committed_still_counts_as_moved(env):
    from unittest.mock import patch

    from src.server.repository.tenant import LibraryRepository

    client, headers, *_ = env
    sha = _sha()
    original = _ingest(env, "aftercommit/A.mov", sha=sha)
    _ingest(env, "aftercommit/copy/A.mov", sha=sha)

    def boom(self, library_id):
        raise RuntimeError("revision bump failed")

    with patch.object(LibraryRepository, "bump_revision", boom):
        r = client.request("DELETE", "/v1/assets", json={"asset_ids": [original], "reason": "missing"},
                           headers=headers)
    assert r.status_code == 200, r.text
    assert _get(env, original)["rel_path"] == "aftercommit/copy/A.mov"
    assert r.json()["handed_over"] == [original]
