"""The daily sweep (src/server/search/cleanup.py): the stored files no row
names, removed through the Store a library at a time, and the folders the
database doesn't list, reported (src/server/storage/folders.py).

Over a real FileStore in the test's folder. The account's database is a
stand-in answering the sweep's queries; the real one: test_artifact_leaks.py,
test_analysis_proxy_api.py, test_processing_pause_api.py.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.server.search.cleanup import (
    CleanupResult,
    remove_orphan_folders,
    run_cleanup_all_tenants,
    run_cleanup_for_tenant,
    run_cleanup_single_tenant,
)
from src.server.storage import store as stored
from src.server.storage.files import PARTIAL
from src.server.storage.local import LocalStorage
from src.shared import previews_location
from tests.stored_in import stored_in

pytestmark = pytest.mark.fast

T, LIB, OWN = "ten_AAAA", "lib_BBBB", "lib_OWN"
OLD = 7200  # seconds: past the sweep's hour


def _put(root: Path, key: str, body: bytes = b"x", *, age: float = OLD) -> Path:
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    then = time.time() - age
    os.utime(path, (then, then))
    return path


def _proxy(name: object, library_id: str = LIB, tenant_id: str = T) -> str:
    return f"{tenant_id}/{library_id}/proxies/00/ast_{name}_photo.webp"


class _Db:
    """An account's database as the sweep asks it: its libraries, and each
    one's keys (any of the clip's key columns), scenes and faces."""

    def __init__(self, libraries, *, keys=None, scenes=None, faces=None, assets=(), listed_later=(),
                 broken=False) -> None:
        self.libraries = list(libraries)
        self.keys = keys or {}
        self.scenes = scenes or {}
        self.faces = faces or {}
        self.assets = list(assets)
        self.listed_later = set(listed_later)  # found by the check just before a removal
        self.broken = broken  # the clips' keys can't be read

    def execute(self, sql, params=None):
        sql, params = str(sql), params or {}
        library_id = params.get("lib_id")
        if "WHERE library_id = :id" in sql:
            listed = params["id"] in self.libraries or params["id"] in self.listed_later
            return SimpleNamespace(first=lambda: (1,) if listed else None)
        if "FROM video_scenes" in sql:
            rows = self.scenes.get(library_id, [])
        elif "FROM faces" in sql:
            rows = self.faces.get(library_id, [])
        elif "proxy_key" in sql:
            if self.broken:
                raise RuntimeError("DB connection lost")
            rows = [(key, None, None, None) for key in self.keys.get(library_id, [])]
        elif "FROM assets" in sql:
            rows = [(a,) for a in self.assets]
        elif "FROM libraries" in sql:
            rows = [(lib,) for lib in self.libraries]
        else:
            raise AssertionError(f"A query the sweep doesn't make: {sql}")
        return SimpleNamespace(fetchall=lambda: rows)


@pytest.fixture
def data(tmp_path) -> Iterator[Path]:
    """The install's DATA_DIR, where its previews are; Upkeep isn't paused (test_processing_pause_api.py)."""
    data = tmp_path / "data"
    data.mkdir()
    with stored_in(LocalStorage(str(data))), patch("src.server.search.cleanup._paused", return_value=False):
        yield data


@pytest.fixture
def drive(data, tmp_path) -> Path:
    """OWN's folder of its own, on a drive that's plugged in."""
    drive = tmp_path / "Volumes" / "Clips"
    drive.mkdir(parents=True)
    (drive / previews_location.MARKER).write_text("")
    previews_location.write(data, data, None, libraries={OWN: drive})
    return drive


@contextmanager
def _accounts(dbs: dict[str, _Db]):
    """The control plane lists these accounts, each with its database."""

    @contextmanager
    def tenant_session(tenant_id):
        yield dbs[tenant_id]

    repo = MagicMock()
    repo.return_value.list_all.return_value = [SimpleNamespace(tenant_id=t) for t in dbs]
    with (
        patch("src.server.database.get_control_session"),
        patch("src.server.database.get_tenant_session", tenant_session),
        patch("src.server.repository.control_plane.TenantRepository", repo),
    ):
        yield


# -- Files no row names ------------------------------------------------------------


def test_the_files_rows_name_stay(data):
    keys = [_proxy(n) for n in range(3)]
    for key in keys:
        _put(data, key)
    assert run_cleanup_for_tenant(T, _Db([LIB], keys={LIB: keys}), dry_run=False) == CleanupResult()
    assert all((data / key).exists() for key in keys)


@pytest.mark.parametrize("dry_run", [True, False])
def test_a_file_no_row_names_goes(data, dry_run):
    known = [_proxy(n) for n in range(10)]
    for key in known:
        _put(data, key)
    orphan = _put(data, _proxy("ORPHAN"), b"orphan")

    result = run_cleanup_for_tenant(T, _Db([LIB], keys={LIB: known}), dry_run=dry_run)

    assert (result.orphan_files, result.bytes_freed, result.errors) == (1, 6, [])
    assert orphan.exists() is dry_run
    assert all((data / key).exists() for key in known)


def test_a_file_under_an_hour_old_stays(data):
    """It may be mid-ingest: written before its row."""
    known = [_proxy(n) for n in range(10)]
    for key in known:
        _put(data, key)
    new = _put(data, _proxy("NEW"), age=10)
    assert run_cleanup_for_tenant(T, _Db([LIB], keys={LIB: known}), dry_run=False).orphan_files == 0
    assert new.exists()


def test_a_library_over_a_quarter_of_whose_files_would_go_is_skipped(data):
    orphans = [_put(data, _proxy(f"O{n}")) for n in range(4)]
    result = run_cleanup_for_tenant(T, _Db([LIB]), dry_run=False)
    assert (result.orphan_files, result.skipped_libraries) == (0, 1)
    [error] = result.errors
    assert "safety threshold" in error and LIB in error
    assert all(p.exists() for p in orphans)


def test_a_library_whose_rows_can_t_be_read_is_skipped(data):
    path = _put(data, _proxy(1))
    result = run_cleanup_for_tenant(T, _Db([LIB], broken=True), dry_run=False)
    assert (result.orphan_files, result.skipped_libraries) == (0, 1)
    assert path.exists()


def test_a_trashed_library_s_files_stay(data):
    """A trashed library still has its row, and its clips theirs."""
    key = _proxy(1, "lib_TRASHED")
    _put(data, key)
    result = run_cleanup_for_tenant(T, _Db(["lib_TRASHED"], keys={"lib_TRASHED": [key]}), dry_run=False)
    assert result == CleanupResult()
    assert (data / key).exists()


def test_only_what_lumiverb_keeps_under_a_library_is_looked_at(data):
    known = [_proxy(n) for n in range(4)]
    for key in known:
        _put(data, key)
    strays = [_put(data, f"{T}/{LIB}/notes.txt"), _put(data, f"{T}/{LIB}/other/00/x.webp"),
              _put(data, f"{T}/random_dir/file.txt")]
    result = run_cleanup_for_tenant(T, _Db([LIB], keys={LIB: known}), dry_run=False)
    assert result == CleanupResult()
    assert all(p.exists() for p in strays)


def test_every_kind_is_looked_at_and_scene_frames_and_crops_are_known_by_their_rows(data):
    """Scene frames and face crops are named by no column of a clip's: the
    sweep makes their keys from the scene's and the face's rows
    (store.scene_rep_key, store.face_crop_key)."""
    asset_x, asset_y = "ast_01HX0000000000000000000001", "ast_01HY0000000000000000000002"
    face = "face_01HY0000000000000000000002"
    named = [stored.proxy_key(T, LIB, asset_x, "photo.jpg"), stored.thumbnail_key(T, LIB, asset_x, "photo.jpg"),
             stored.video_preview_key(T, LIB, asset_y, "video.mp4"),
             stored.analysis_proxy_key(T, LIB, asset_y, "video.mp4")]
    made = [stored.scene_rep_key(T, LIB, asset_y, 1000), stored.face_crop_key(T, LIB, face)]
    orphans = [stored.thumbnail_key(T, LIB, asset_x, "orphan.jpg"),
               stored.face_crop_key(T, LIB, "face_01HZ0000000000000000000009")]
    for key in [*named, *made, *orphans]:
        _put(data, key)
    assert {stored.kind_of(k) for k in [*named, *made]} == set(stored.KINDS)

    db = _Db([LIB], keys={LIB: named}, scenes={LIB: [(asset_y, 1000, None, None)]}, faces={LIB: [(face, None)]})
    result = run_cleanup_for_tenant(T, db, dry_run=False)

    assert result.orphan_files == 2, result.errors
    assert not any((data / k).exists() for k in orphans)
    assert all((data / k).exists() for k in [*named, *made])


def test_scene_frames_no_column_names_are_never_taken_for_orphans(data):
    """Regression: before the sweep made scene frames' keys, every one was
    flagged; 3 of 20 is under the cap, so they'd have gone."""
    asset_id = "ast_01HZ0000000000000000000003"
    frames = [stored.scene_rep_key(T, LIB, asset_id, ms) for ms in (1000, 5000, 9000)]
    proxies = [stored.proxy_key(T, LIB, asset_id, f"x{i}.jpg") for i in range(17)]
    for key in frames + proxies:
        _put(data, key)
    db = _Db([LIB], keys={LIB: proxies}, scenes={LIB: [(asset_id, ms, None, None) for ms in (1000, 5000, 9000)]})
    result = run_cleanup_for_tenant(T, db, dry_run=False)
    assert (result.orphan_files, result.skipped_libraries) == (0, 0), result.errors
    assert all((data / k).exists() for k in frames)


@pytest.mark.parametrize("dry_run", [True, False])
def test_a_write_a_crash_left_goes_once_it_s_an_hour_old(data, dry_run):
    """A write that ends removes its partial file (files.PARTIAL), and no
    listing shows one: one an hour untouched was left by a crash."""
    known = [_proxy(n) for n in range(4)]
    for key in known:
        _put(data, key)
    left = _put(data, f"{T}/{LIB}/analysis/00/{PARTIAL}0123456789abcdef.mp4", b"half")
    writing = _put(data, f"{T}/{LIB}/analysis/00/{PARTIAL}fedcba9876543210.mp4", b"half", age=10)

    result = run_cleanup_for_tenant(T, _Db([LIB], keys={LIB: known}), dry_run=dry_run)

    assert (result.orphan_files, result.bytes_freed) == (1, 4)
    assert left.exists() is dry_run and writing.exists()


# -- A library's folder of its own ---------------------------------------------------


def test_a_library_with_a_folder_of_its_own_is_swept_there(data, drive):
    own, ours = [_proxy(n, OWN) for n in range(10)], [_proxy(n) for n in range(10)]
    for key in own:
        _put(drive, key)
    for key in ours:
        _put(data, key)
    orphans = [_put(drive, _proxy("ORPHAN", OWN)), _put(data, _proxy("ORPHAN"))]

    result = run_cleanup_for_tenant(T, _Db([LIB, OWN], keys={LIB: ours, OWN: own}), dry_run=False)

    assert (result.orphan_files, result.errors) == (2, [])
    assert not any(p.exists() for p in orphans)
    assert all((drive / k).exists() for k in own) and all((data / k).exists() for k in ours)


def test_a_library_whose_drive_isn_t_plugged_in_is_skipped_and_says_so(data, drive):
    """Nothing on a drive that isn't there is taken for orphaned; the other
    libraries are swept as ever."""
    own, ours = [_proxy(n, OWN) for n in range(10)], [_proxy(n) for n in range(10)]
    for key in own:
        _put(drive, key)
    for key in ours:
        _put(data, key)
    theirs, mine = _put(drive, _proxy("ORPHAN", OWN)), _put(data, _proxy("ORPHAN"))
    (drive / previews_location.MARKER).unlink()

    result = run_cleanup_for_tenant(T, _Db([LIB, OWN], keys={LIB: ours, OWN: own}), dry_run=False)

    assert result.errors == [f"Skipped library {OWN}: its previews can't be reached now (is its drive plugged in?)"]
    assert (result.orphan_files, result.skipped_libraries) == (1, 1)
    assert not mine.exists() and theirs.exists()
    (drive / previews_location.MARKER).write_text("")
    assert all((drive / k).exists() for k in own)


def test_one_library_is_swept_alone(data, drive):
    _put(drive, _proxy("ORPHAN", OWN))
    for n in range(10):
        _put(drive, _proxy(n, OWN))
    mine = _put(data, _proxy("ORPHAN"))
    _put(data, f"{T}/lib_GONE/proxies/00/ast_1_photo.webp")
    db = _Db([LIB, OWN], keys={OWN: [_proxy(n, OWN) for n in range(10)]})

    result = run_cleanup_single_tenant(T, db, dry_run=False, library_id=OWN)

    assert (result.orphan_files, result.orphan_folders, result.errors) == (1, [], [])
    assert mine.exists(), "another library's are left for its own run"


# -- Folders the database doesn't list -------------------------------------------------


def test_library_folders_the_database_doesn_t_list_are_reported_and_kept(data, caplog):
    """A database restored from an older backup (or empty) lists none of the
    newer libraries: their folders are reported, never removed (Robert)."""
    paths = [_put(data, _proxy("X", lib), b"xyz") for lib in ("lib_A", "lib_B")]

    with caplog.at_level("WARNING", logger="src.server.search.cleanup"):
        result = run_cleanup_single_tenant(T, _Db([]), dry_run=False)

    assert all(p.exists() for p in paths)
    assert sorted((f["folder_id"], f["path"], f["bytes"]) for f in result.orphan_folders) == [
        ("lib_A", f"{T}/lib_A", 3), ("lib_B", f"{T}/lib_B", 3)]
    assert all(f["newest_mtime"] for f in result.orphan_folders)
    [warning] = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert "2 folder(s)" in warning and "lib_A" in warning and "lib_B" in warning


def test_a_library_deleted_while_its_drive_was_away_is_reported_when_it_s_back(data, drive):
    """Its folder stays, and its choice (move.forget_library): when the drive
    is back the sweep reports it, and removing it takes the choice too."""
    _put(drive, _proxy("X", OWN), b"xyz")

    result = run_cleanup_single_tenant(T, _Db([LIB]), dry_run=False)
    assert [(f["folder_id"], f["path"], f["bytes"]) for f in result.orphan_folders] == [(OWN, f"{T}/{OWN}", 3)]
    assert str(drive) not in str(result), "a folder is named by what it holds, never where it is"

    out = remove_orphan_folders(tenant_id=T, session=_Db([LIB]), folder_ids=[OWN], dry_run=False)
    assert [f["folder_id"] for f in out.removed] == [OWN] and out.errors == []
    assert not (drive / T / OWN).exists()
    assert previews_location.read(data).libraries == {}


def test_a_linked_folder_is_neither_reported_nor_removed(data, tmp_path):
    """A link in the previews folder is an operator's: nothing is ever removed through one."""
    elsewhere = tmp_path / "elsewhere"
    kept = _put(elsewhere, "proxies/00/ast_1_photo.webp")
    (data / T).mkdir()
    (data / T / "lib_LINK").symlink_to(elsewhere)
    assert run_cleanup_single_tenant(T, _Db([]), dry_run=False).orphan_folders == []
    out = remove_orphan_folders(tenant_id=T, session=_Db([]), folder_ids=["lib_LINK"], dry_run=False)
    assert (out.removed, out.not_found) == ([], ["lib_LINK"])
    assert kept.exists()


def test_account_folders_the_control_plane_doesn_t_list_are_reported(data, caplog):
    _put(data, "ten_A/x")
    _put(data, _proxy(1, "lib_Z", "ten_B"))
    with _accounts({"ten_B": _Db([])}), caplog.at_level("WARNING", logger="src.server.search.cleanup"):
        result = run_cleanup_all_tenants(dry_run=False)
    assert sorted(f["path"] for f in result.orphan_folders) == ["ten_A", "ten_B/lib_Z"]
    assert (data / "ten_A").exists() and (data / "ten_B" / "lib_Z").exists()
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1  # one per run


def test_remove_orphan_folders_removes_only_what_is_named(data):
    gone, other, known = (_put(data, _proxy("X", lib), b"xyz") for lib in ("lib_GONE", "lib_OTHER", "lib_KNOWN"))

    dry = remove_orphan_folders(tenant_id=T, session=_Db(["lib_KNOWN"]), folder_ids=["lib_GONE"], dry_run=True)
    assert [f["folder_id"] for f in dry.removed] == ["lib_GONE"] and gone.exists()

    out = remove_orphan_folders(tenant_id=T, session=_Db(["lib_KNOWN"]),
                                folder_ids=["lib_GONE", "lib_KNOWN", "lib_NOPE"], dry_run=False)
    assert [f["folder_id"] for f in out.removed] == ["lib_GONE"] and out.bytes_freed == 3
    assert out.not_found == ["lib_KNOWN", "lib_NOPE"]
    assert not gone.exists() and other.exists() and known.exists()


def test_remove_orphan_folders_checks_the_database_again_before_each(data):
    """A library the database lists by the time its folder's turn comes is kept."""
    a, b = _put(data, _proxy("X", "lib_A")), _put(data, _proxy("X", "lib_B"))
    out = remove_orphan_folders(tenant_id=T, session=_Db([], listed_later=("lib_A",)), folder_ids=None,
                                dry_run=False)
    assert [f["folder_id"] for f in out.removed] == ["lib_B"]
    assert a.exists() and not b.exists()


def test_remove_orphan_folders_of_every_account_takes_gone_accounts(data):
    _put(data, "ten_GONE/x")
    kept = _put(data, _proxy("X", "lib_KNOWN", "ten_LIVE"))
    with _accounts({"ten_LIVE": _Db(["lib_KNOWN"])}):
        out = remove_orphan_folders(tenant_id=None, session=None, folder_ids=None, dry_run=False)
    assert [f["folder_id"] for f in out.removed] == ["ten_GONE"]
    assert not (data / "ten_GONE").exists() and kept.exists()
