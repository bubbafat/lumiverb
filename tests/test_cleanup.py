"""Tests for filesystem cleanup logic (src/search/cleanup.py)."""

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.server.search.cleanup import (
    CleanupResult,
    _MAX_DELETE_FRACTION,
    _MIN_AGE_SECONDS,
    _file_age_seconds,
    _list_subdirs,
    _walk_files,
    run_cleanup_for_tenant,
)


# ---------------------------------------------------------------------------
# Helper: build a fake on-disk layout
# ---------------------------------------------------------------------------

def _make_file(path: Path, content: bytes = b"x", age_seconds: float = 7200) -> None:
    """Create a file and set its mtime to `age_seconds` ago."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    old_time = time.time() - age_seconds
    import os
    os.utime(path, (old_time, old_time))


def _setup_library_dir(
    data_dir: Path,
    tenant_id: str,
    library_id: str,
    proxy_keys: list[str],
    *,
    age_seconds: float = 7200,
) -> None:
    """Create proxy files on disk under data_dir/tenant_id/library_id/proxies/."""
    for key in proxy_keys:
        _make_file(data_dir / key, age_seconds=age_seconds)


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------

@pytest.mark.fast
def test_list_subdirs(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "file.txt").write_text("x")
    result = _list_subdirs(tmp_path)
    assert sorted(result) == ["a", "b"]


@pytest.mark.fast
def test_list_subdirs_missing_dir() -> None:
    result = _list_subdirs(Path("/nonexistent"))
    assert result == []


@pytest.mark.fast
def test_walk_files(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "sub" / "b.txt").write_text("y")
    result = _walk_files(tmp_path)
    names = sorted(f.name for f in result)
    assert names == ["a.txt", "b.txt"]


@pytest.mark.fast
def test_walk_files_missing_dir() -> None:
    result = _walk_files(Path("/nonexistent"))
    assert result == []


@pytest.mark.fast
def test_file_age_seconds(tmp_path: Path) -> None:
    f = tmp_path / "test.txt"
    f.write_text("x")
    age = _file_age_seconds(f)
    assert 0 <= age < 5  # just created


# ---------------------------------------------------------------------------
# Cleanup for a single tenant
# ---------------------------------------------------------------------------

def _mock_session_with_libraries(library_ids: list[str], asset_keys: dict[str, list[str]]):
    """Build a mock Session that returns library_ids and asset keys.

    asset_keys: {library_id: [key1, key2, ...]}
    """
    session = MagicMock()

    def execute_side_effect(stmt, params=None):
        result = MagicMock()
        sql = str(stmt.text if hasattr(stmt, "text") else stmt)

        if "FROM libraries" in sql:
            rows = [MagicMock(**{"__getitem__": lambda self, i, lid=lid: lid}) for lid in library_ids]
            # Make row[0] return library_id
            for row, lid in zip(rows, library_ids):
                row.__getitem__ = lambda self, i, lid=lid: lid
            result.fetchall.return_value = rows
        elif "FROM assets" in sql:
            lib_id = params.get("lib_id", "") if params else ""
            keys = asset_keys.get(lib_id, [])
            rows = []
            for key in keys:
                row = MagicMock()
                row.__iter__ = lambda self, k=key: iter([k, None, None])
                rows.append(row)
            result.fetchall.return_value = rows
        elif "FROM video_scenes" in sql:
            result.fetchall.return_value = []
        else:
            result.fetchall.return_value = []

        return result

    session.execute.side_effect = execute_side_effect
    return session


@pytest.mark.fast
def test_cleanup_no_orphans(tmp_path: Path) -> None:
    """All files on disk have matching DB keys — nothing to delete."""
    tenant_id = "ten_AAAA"
    library_id = "lib_BBBB"
    key = f"{tenant_id}/{library_id}/proxies/00/ast_CCCC_photo.webp"

    _setup_library_dir(tmp_path, tenant_id, library_id, [key])

    session = MagicMock()

    def execute_side_effect(stmt, params=None):
        result = MagicMock()
        sql = str(stmt.text if hasattr(stmt, "text") else stmt)
        if "FROM libraries" in sql:
            row = MagicMock()
            row.__getitem__ = lambda self, i: library_id
            result.fetchall.return_value = [row]
        elif "FROM assets" in sql:
            row = MagicMock()
            row.__iter__ = lambda self: iter([key, None, None])
            result.fetchall.return_value = [row]
        elif "FROM video_scenes" in sql:
            result.fetchall.return_value = []
        else:
            result.fetchall.return_value = []
        return result

    session.execute.side_effect = execute_side_effect

    result = run_cleanup_for_tenant(tmp_path, tenant_id, session, dry_run=True)

    assert result.orphan_files == 0
    assert result.orphan_libraries == 0
    assert result.bytes_freed == 0
    # File still exists
    assert (tmp_path / key).exists()


@pytest.mark.fast
def test_cleanup_orphan_file_dry_run(tmp_path: Path) -> None:
    """An orphan file is detected in dry-run but not deleted."""
    tenant_id = "ten_AAAA"
    library_id = "lib_BBBB"
    orphan_key = f"{tenant_id}/{library_id}/proxies/00/ast_ORPHAN_photo.webp"
    known_key = f"{tenant_id}/{library_id}/proxies/00/ast_KNOWN_photo.webp"

    # Put both files on disk
    _setup_library_dir(tmp_path, tenant_id, library_id, [orphan_key, known_key])
    # Also create some more known files to stay under the 25% threshold
    for i in range(10):
        extra_key = f"{tenant_id}/{library_id}/proxies/00/ast_KNOWN{i}_photo.webp"
        _setup_library_dir(tmp_path, tenant_id, library_id, [extra_key])

    session = MagicMock()

    def execute_side_effect(stmt, params=None):
        result = MagicMock()
        sql = str(stmt.text if hasattr(stmt, "text") else stmt)
        if "FROM libraries" in sql:
            row = MagicMock()
            row.__getitem__ = lambda self, i: library_id
            result.fetchall.return_value = [row]
        elif "FROM assets" in sql:
            # Only known_key and the extras are in DB — orphan_key is missing
            db_keys = [known_key] + [
                f"{tenant_id}/{library_id}/proxies/00/ast_KNOWN{i}_photo.webp"
                for i in range(10)
            ]
            rows = []
            for k in db_keys:
                row = MagicMock()
                row.__iter__ = lambda self, k=k: iter([k, None, None])
                rows.append(row)
            result.fetchall.return_value = rows
        elif "FROM video_scenes" in sql:
            result.fetchall.return_value = []
        else:
            result.fetchall.return_value = []
        return result

    session.execute.side_effect = execute_side_effect

    result = run_cleanup_for_tenant(tmp_path, tenant_id, session, dry_run=True)

    assert result.orphan_files == 1
    assert result.bytes_freed > 0
    # File still exists (dry-run)
    assert (tmp_path / orphan_key).exists()


@pytest.mark.fast
def test_cleanup_orphan_file_execute(tmp_path: Path) -> None:
    """An orphan file is actually deleted when dry_run=False."""
    tenant_id = "ten_AAAA"
    library_id = "lib_BBBB"
    orphan_key = f"{tenant_id}/{library_id}/proxies/00/ast_ORPHAN_photo.webp"

    # Create enough known files to keep orphan under 25%
    known_keys = [
        f"{tenant_id}/{library_id}/proxies/00/ast_KNOWN{i}_photo.webp"
        for i in range(10)
    ]
    all_keys = [orphan_key] + known_keys
    _setup_library_dir(tmp_path, tenant_id, library_id, all_keys)

    session = MagicMock()

    def execute_side_effect(stmt, params=None):
        result = MagicMock()
        sql = str(stmt.text if hasattr(stmt, "text") else stmt)
        if "FROM libraries" in sql:
            row = MagicMock()
            row.__getitem__ = lambda self, i: library_id
            result.fetchall.return_value = [row]
        elif "FROM assets" in sql:
            rows = []
            for k in known_keys:
                row = MagicMock()
                row.__iter__ = lambda self, k=k: iter([k, None, None])
                rows.append(row)
            result.fetchall.return_value = rows
        elif "FROM video_scenes" in sql:
            result.fetchall.return_value = []
        else:
            result.fetchall.return_value = []
        return result

    session.execute.side_effect = execute_side_effect

    result = run_cleanup_for_tenant(tmp_path, tenant_id, session, dry_run=False)

    assert result.orphan_files == 1
    assert not (tmp_path / orphan_key).exists()
    # Known files still exist
    for k in known_keys:
        assert (tmp_path / k).exists()


@pytest.mark.fast
def test_cleanup_orphan_library_dir(tmp_path: Path) -> None:
    """A library dir with no matching DB row is removed."""
    tenant_id = "ten_AAAA"
    orphan_lib = "lib_ORPHAN"
    orphan_file = f"{tenant_id}/{orphan_lib}/proxies/00/ast_X_photo.webp"
    _setup_library_dir(tmp_path, tenant_id, orphan_lib, [orphan_file])

    session = MagicMock()

    def execute_side_effect(stmt, params=None):
        result = MagicMock()
        sql = str(stmt.text if hasattr(stmt, "text") else stmt)
        if "FROM libraries" in sql:
            result.fetchall.return_value = []  # no libraries in DB
        else:
            result.fetchall.return_value = []
        return result

    session.execute.side_effect = execute_side_effect

    result = run_cleanup_for_tenant(tmp_path, tenant_id, session, dry_run=False)

    assert result.orphan_libraries == 1
    assert result.bytes_freed > 0
    assert not (tmp_path / tenant_id / orphan_lib).exists()


@pytest.mark.fast
def test_cleanup_skips_new_files(tmp_path: Path) -> None:
    """Files newer than the age threshold are not deleted."""
    tenant_id = "ten_AAAA"
    library_id = "lib_BBBB"
    new_orphan = f"{tenant_id}/{library_id}/proxies/00/ast_NEW_photo.webp"

    # Create file with age = 10 seconds (below 1hr threshold)
    _setup_library_dir(tmp_path, tenant_id, library_id, [new_orphan], age_seconds=10)

    session = MagicMock()

    def execute_side_effect(stmt, params=None):
        result = MagicMock()
        sql = str(stmt.text if hasattr(stmt, "text") else stmt)
        if "FROM libraries" in sql:
            row = MagicMock()
            row.__getitem__ = lambda self, i: library_id
            result.fetchall.return_value = [row]
        elif "FROM assets" in sql:
            result.fetchall.return_value = []  # no assets in DB
        elif "FROM video_scenes" in sql:
            result.fetchall.return_value = []
        else:
            result.fetchall.return_value = []
        return result

    session.execute.side_effect = execute_side_effect

    result = run_cleanup_for_tenant(tmp_path, tenant_id, session, dry_run=False)

    assert result.orphan_files == 0
    # File still exists because it's too new
    assert (tmp_path / new_orphan).exists()


@pytest.mark.fast
def test_cleanup_safety_cap_aborts_library(tmp_path: Path) -> None:
    """If >25% of files would be deleted, skip the library."""
    tenant_id = "ten_AAAA"
    library_id = "lib_BBBB"

    # Create 4 files, all orphans (100% > 25%)
    orphan_keys = [
        f"{tenant_id}/{library_id}/proxies/00/ast_ORPHAN{i}_photo.webp"
        for i in range(4)
    ]
    _setup_library_dir(tmp_path, tenant_id, library_id, orphan_keys)

    session = MagicMock()

    def execute_side_effect(stmt, params=None):
        result = MagicMock()
        sql = str(stmt.text if hasattr(stmt, "text") else stmt)
        if "FROM libraries" in sql:
            row = MagicMock()
            row.__getitem__ = lambda self, i: library_id
            result.fetchall.return_value = [row]
        elif "FROM assets" in sql:
            result.fetchall.return_value = []  # none in DB
        elif "FROM video_scenes" in sql:
            result.fetchall.return_value = []
        else:
            result.fetchall.return_value = []
        return result

    session.execute.side_effect = execute_side_effect

    result = run_cleanup_for_tenant(tmp_path, tenant_id, session, dry_run=False)

    assert result.orphan_files == 0  # nothing deleted
    assert result.skipped_libraries == 1
    assert len(result.errors) == 1
    assert "safety threshold" in result.errors[0]
    # All files still exist
    for k in orphan_keys:
        assert (tmp_path / k).exists()


@pytest.mark.fast
def test_cleanup_db_error_skips_library(tmp_path: Path) -> None:
    """If DB query fails for a library, it is skipped."""
    tenant_id = "ten_AAAA"
    library_id = "lib_BBBB"
    key = f"{tenant_id}/{library_id}/proxies/00/ast_X_photo.webp"
    _setup_library_dir(tmp_path, tenant_id, library_id, [key])

    call_count = 0
    session = MagicMock()

    def execute_side_effect(stmt, params=None):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        sql = str(stmt.text if hasattr(stmt, "text") else stmt)
        if "FROM libraries" in sql:
            row = MagicMock()
            row.__getitem__ = lambda self, i: library_id
            result.fetchall.return_value = [row]
            return result
        elif "FROM assets" in sql:
            raise RuntimeError("DB connection lost")
        return result

    session.execute.side_effect = execute_side_effect

    result = run_cleanup_for_tenant(tmp_path, tenant_id, session, dry_run=False)

    assert result.orphan_files == 0
    assert result.skipped_libraries == 1
    assert (tmp_path / key).exists()


@pytest.mark.fast
def test_cleanup_trashed_library_preserved(tmp_path: Path) -> None:
    """A trashed library still has a DB row, so its dir should NOT be deleted."""
    tenant_id = "ten_AAAA"
    trashed_lib = "lib_TRASHED"
    key = f"{tenant_id}/{trashed_lib}/proxies/00/ast_X_photo.webp"
    _setup_library_dir(tmp_path, tenant_id, trashed_lib, [key])

    session = MagicMock()

    def execute_side_effect(stmt, params=None):
        result = MagicMock()
        sql = str(stmt.text if hasattr(stmt, "text") else stmt)
        if "FROM libraries" in sql:
            # Trashed library still appears in DB
            row = MagicMock()
            row.__getitem__ = lambda self, i: trashed_lib
            result.fetchall.return_value = [row]
        elif "FROM assets" in sql:
            row = MagicMock()
            row.__iter__ = lambda self: iter([key, None, None])
            result.fetchall.return_value = [row]
        elif "FROM video_scenes" in sql:
            result.fetchall.return_value = []
        else:
            result.fetchall.return_value = []
        return result

    session.execute.side_effect = execute_side_effect

    result = run_cleanup_for_tenant(tmp_path, tenant_id, session, dry_run=False)

    assert result.orphan_libraries == 0
    assert result.orphan_files == 0
    assert (tmp_path / key).exists()


@pytest.mark.fast
def test_cleanup_no_tenant_dir(tmp_path: Path) -> None:
    """If tenant dir doesn't exist on disk, returns empty result."""
    session = MagicMock()
    result = run_cleanup_for_tenant(tmp_path, "ten_NONEXISTENT", session, dry_run=True)
    assert result == CleanupResult()


@pytest.mark.fast
def test_cleanup_ignores_non_lib_dirs(tmp_path: Path) -> None:
    """Directories not starting with lib_ are ignored."""
    tenant_id = "ten_AAAA"
    (tmp_path / tenant_id / "random_dir").mkdir(parents=True)
    (tmp_path / tenant_id / "random_dir" / "file.txt").write_text("x")

    session = MagicMock()

    def execute_side_effect(stmt, params=None):
        result = MagicMock()
        result.fetchall.return_value = []
        return result

    session.execute.side_effect = execute_side_effect

    result = run_cleanup_for_tenant(tmp_path, tenant_id, session, dry_run=False)

    assert result.orphan_libraries == 0
    assert (tmp_path / tenant_id / "random_dir" / "file.txt").exists()


@pytest.mark.fast
def test_cleanup_handles_multiple_artifact_types(tmp_path: Path) -> None:
    """Cleanup checks proxies, thumbnails, previews, and scenes subdirs.

    Critically, scene_rep JPGs (under /scenes/) are not stored in any DB column —
    they live at a deterministic path derived from (tenant_id, library_id,
    asset_id, rep_frame_ms) by LocalStorage.scene_rep_key(). Cleanup must
    compute that path from video_scenes rows and treat the file as expected.
    A previous version of this test stuffed the scene path into the
    video_scenes.proxy_key column slot, which masked a bug where cleanup had
    no idea about scene_rep JPGs at all.
    """
    from src.server.storage.local import LocalStorage

    tenant_id = "ten_AAAA"
    library_id = "lib_BBBB"
    # Real ULIDs — scene_rep_key() decodes them for bucketing.
    asset_x = "ast_01HX0000000000000000000001"
    asset_y = "ast_01HY0000000000000000000002"
    rep_frame_ms = 1000

    storage = LocalStorage(str(tmp_path))
    proxy_key = storage.proxy_key(tenant_id, library_id, asset_x, "photo.jpg")
    thumb_key = storage.thumbnail_key(tenant_id, library_id, asset_x, "photo.jpg")
    preview_key = storage.video_preview_key(tenant_id, library_id, asset_y, "video.mp4")
    scene_key = storage.scene_rep_key(tenant_id, library_id, asset_y, rep_frame_ms)
    orphan_key = storage.thumbnail_key(tenant_id, library_id, asset_x, "orphan.jpg")
    # Force the orphan key into a different filename so it's distinct from thumb_key.
    assert orphan_key != thumb_key

    all_keys = [proxy_key, thumb_key, preview_key, scene_key, orphan_key]
    _setup_library_dir(tmp_path, tenant_id, library_id, all_keys)

    session = MagicMock()

    def execute_side_effect(stmt, params=None):
        result = MagicMock()
        sql = str(stmt.text if hasattr(stmt, "text") else stmt)
        if "FROM libraries" in sql:
            row = MagicMock()
            row.__getitem__ = lambda self, i: library_id
            result.fetchall.return_value = [row]
        elif "FROM assets" in sql:
            # Real assets row shape: (proxy_key, thumbnail_key, video_preview_key)
            result.fetchall.return_value = [
                (proxy_key, thumb_key, preview_key),
            ]
        elif "FROM video_scenes" in sql:
            # Real video_scenes row shape after the cleanup fix:
            # (asset_id, rep_frame_ms, proxy_key, thumbnail_key)
            # The scene_rep JPG path is NOT in this row — cleanup must
            # compute it itself via storage.scene_rep_key(...).
            result.fetchall.return_value = [
                (asset_y, rep_frame_ms, None, None),
            ]
        else:
            result.fetchall.return_value = []
        return result

    session.execute.side_effect = execute_side_effect

    result = run_cleanup_for_tenant(tmp_path, tenant_id, session, dry_run=False)

    assert result.orphan_files == 1, (
        f"expected exactly 1 orphan but got {result.orphan_files}; "
        f"scene_rep JPG may have been mis-classified as orphan"
    )
    assert not (tmp_path / orphan_key).exists()
    for k in [proxy_key, thumb_key, preview_key, scene_key]:
        assert (tmp_path / k).exists(), f"{k} should not have been deleted"


@pytest.mark.fast
def test_cleanup_preserves_scene_rep_jpgs(tmp_path: Path) -> None:
    """Regression: a scene_rep JPG with a matching video_scenes row must not
    be flagged as orphaned, even though no DB column stores its path.

    Before the fix to _get_expected_keys_for_library, this test would have
    failed: cleanup had no awareness of scene_rep JPG paths and would have
    flagged every one of them. The bug was hidden in production by an
    unrelated bug in artifacts.py that accidentally stored the scene path in
    assets.video_preview_key, putting it in the expected-keys set by accident.
    """
    from src.server.storage.local import LocalStorage

    tenant_id = "ten_AAAA"
    library_id = "lib_BBBB"
    asset_id = "ast_01HZ0000000000000000000003"
    storage = LocalStorage(str(tmp_path))

    # Three scene_rep JPGs + enough proxies so that *if* the JPGs were
    # mis-flagged as orphans, the 25% safety cap would not save them
    # (3 / 20 = 15% < 25%) and they would actually be deleted.
    rep_frame_msses = [1000, 5000, 9000]
    scene_keys = [
        storage.scene_rep_key(tenant_id, library_id, asset_id, ms)
        for ms in rep_frame_msses
    ]
    proxy_keys = [
        storage.proxy_key(tenant_id, library_id, asset_id, f"x{i}.jpg")
        for i in range(17)
    ]
    _setup_library_dir(tmp_path, tenant_id, library_id, scene_keys + proxy_keys)

    session = MagicMock()

    def execute_side_effect(stmt, params=None):
        result = MagicMock()
        sql = str(stmt.text if hasattr(stmt, "text") else stmt)
        if "FROM libraries" in sql:
            row = MagicMock()
            row.__getitem__ = lambda self, i: library_id
            result.fetchall.return_value = [row]
        elif "FROM assets" in sql:
            # All proxy keys are tracked; no preview/thumbnail.
            result.fetchall.return_value = [(k, None, None) for k in proxy_keys]
        elif "FROM video_scenes" in sql:
            result.fetchall.return_value = [
                (asset_id, ms, None, None) for ms in rep_frame_msses
            ]
        else:
            result.fetchall.return_value = []
        return result

    session.execute.side_effect = execute_side_effect

    result = run_cleanup_for_tenant(tmp_path, tenant_id, session, dry_run=False)

    assert result.orphan_files == 0, (
        f"scene_rep JPGs were mis-classified as orphans: got {result.orphan_files}"
    )
    # Belt-and-braces: also assert the safety cap did not trip. Without the
    # cleanup fix this assertion would also fail (the helper would skip the
    # library because >25% of files would have been deleted).
    assert result.skipped_libraries == 0, (
        f"library skipped instead of recognising scene_rep JPGs: {result.errors}"
    )
    for k in scene_keys:
        assert (tmp_path / k).exists(), f"scene_rep JPG was deleted: {k}"
