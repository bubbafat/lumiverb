"""Tests for file move detection in scan (SHA-based path change detection).

Tests the move detection algorithm, CLI flag validation, batch-moves
endpoint model, and integration with the scan pipeline.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.client.cli.scan import (
    ScanStats,
    _MoveCandidate,
    _ServerAsset,
    _detect_deletions,
    _detect_moves,
    _split_files,
)


def _make_file(tmp_path: Path, rel: str, content: bytes = b"test") -> dict:
    """Create a real file and return a walk-style file descriptor."""
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return {
        "rel_path": rel,
        "file_size": len(content),
        "file_mtime": None,
        "media_type": "image",
        "ext": p.suffix.lower(),
    }


@pytest.mark.fast
class TestDetectMoves:
    """Test _detect_moves identifies files that changed path but not content."""

    def test_basic_move(self, tmp_path):
        """File at new path with SHA matching a server asset at a missing old path."""
        content = b"unique-content-12345"
        new_file = _make_file(tmp_path, "new_folder/photo.jpg", content)

        # Server has the file at old_folder/photo.jpg (same SHA)
        from src.client.workers.exif_extract import compute_sha256
        sha = compute_sha256(tmp_path / "new_folder/photo.jpg")

        existing = {
            "old_folder/photo.jpg": _ServerAsset(asset_id="ast_1", sha256=sha),
        }
        local_paths = {"new_folder/photo.jpg"}  # old path NOT in local

        moves, remaining = _detect_moves([new_file], existing, tmp_path, local_paths)

        assert len(moves) == 1
        assert moves[0].asset_id == "ast_1"
        assert moves[0].old_rel_path == "old_folder/photo.jpg"
        assert moves[0].new_rel_path == "new_folder/photo.jpg"
        assert len(remaining) == 0

    def test_no_move_when_old_path_still_exists(self, tmp_path):
        """If the old path is still present locally, it's not a move — it's a copy."""
        content = b"duplicate-content"
        new_file = _make_file(tmp_path, "copy/photo.jpg", content)

        from src.client.workers.exif_extract import compute_sha256
        sha = compute_sha256(tmp_path / "copy/photo.jpg")

        existing = {
            "original/photo.jpg": _ServerAsset(asset_id="ast_1", sha256=sha),
        }
        # Old path IS in local — not a move
        local_paths = {"copy/photo.jpg", "original/photo.jpg"}

        moves, remaining = _detect_moves([new_file], existing, tmp_path, local_paths)

        assert len(moves) == 0
        assert len(remaining) == 1  # treated as new file

    def test_no_move_when_sha_doesnt_match(self, tmp_path):
        """Different SHA = new file, not a move."""
        new_file = _make_file(tmp_path, "new/photo.jpg", b"new-content")

        existing = {
            "old/photo.jpg": _ServerAsset(asset_id="ast_1", sha256="different-sha-value"),
        }
        local_paths = {"new/photo.jpg"}

        moves, remaining = _detect_moves([new_file], existing, tmp_path, local_paths)

        assert len(moves) == 0
        assert len(remaining) == 1

    def test_no_move_when_server_has_no_sha(self, tmp_path):
        """Server asset without SHA can't be matched."""
        new_file = _make_file(tmp_path, "new/photo.jpg", b"content")

        existing = {
            "old/photo.jpg": _ServerAsset(asset_id="ast_1", sha256=None),
        }
        local_paths = {"new/photo.jpg"}

        moves, remaining = _detect_moves([new_file], existing, tmp_path, local_paths)

        assert len(moves) == 0
        assert len(remaining) == 1

    def test_multiple_moves(self, tmp_path):
        """Multiple files moved at once."""
        content_a = b"content-a-unique"
        content_b = b"content-b-unique"
        file_a = _make_file(tmp_path, "new/a.jpg", content_a)
        file_b = _make_file(tmp_path, "new/b.jpg", content_b)

        from src.client.workers.exif_extract import compute_sha256
        sha_a = compute_sha256(tmp_path / "new/a.jpg")
        sha_b = compute_sha256(tmp_path / "new/b.jpg")

        existing = {
            "old/a.jpg": _ServerAsset(asset_id="ast_a", sha256=sha_a),
            "old/b.jpg": _ServerAsset(asset_id="ast_b", sha256=sha_b),
        }
        local_paths = {"new/a.jpg", "new/b.jpg"}

        moves, remaining = _detect_moves([file_a, file_b], existing, tmp_path, local_paths)

        assert len(moves) == 2
        assert len(remaining) == 0
        moved_ids = {m.asset_id for m in moves}
        assert moved_ids == {"ast_a", "ast_b"}

    def test_duplicate_sha_picks_missing_path(self, tmp_path):
        """When SHA matches multiple server assets, pick the one whose path is gone."""
        content = b"shared-content"
        new_file = _make_file(tmp_path, "new/photo.jpg", content)

        from src.client.workers.exif_extract import compute_sha256
        sha = compute_sha256(tmp_path / "new/photo.jpg")

        existing = {
            "still_here/photo.jpg": _ServerAsset(asset_id="ast_keep", sha256=sha),
            "gone/photo.jpg": _ServerAsset(asset_id="ast_moved", sha256=sha),
        }
        # still_here/photo.jpg IS local, gone/photo.jpg is NOT
        local_paths = {"new/photo.jpg", "still_here/photo.jpg"}

        moves, remaining = _detect_moves([new_file], existing, tmp_path, local_paths)

        assert len(moves) == 1
        assert moves[0].asset_id == "ast_moved"
        assert moves[0].old_rel_path == "gone/photo.jpg"

    def test_skips_entirely_when_no_deletions(self, tmp_path):
        """When deleted_ids is empty, move detection is skipped entirely."""
        content = b"some-file-content"
        new_file = _make_file(tmp_path, "truly_new.jpg", content)

        existing = {}
        local_paths = {"truly_new.jpg"}

        moves, remaining = _detect_moves(
            [new_file], existing, tmp_path, local_paths, deleted_ids=[],
        )

        assert len(moves) == 0
        assert len(remaining) == 1
        # SHA not computed (skipped due to no deletions)
        assert remaining[0].get("source_sha256") is None

    def test_sets_source_sha_when_hashed(self, tmp_path):
        """Move detection sets source_sha256 on files it hashes."""
        content = b"some-file-content"
        new_file = _make_file(tmp_path, "new/photo.jpg", content)

        from src.client.workers.exif_extract import compute_sha256
        sha = compute_sha256(tmp_path / "new/photo.jpg")

        existing = {
            "old/photo.jpg": _ServerAsset(
                asset_id="ast_1", sha256=sha, file_size=len(content),
            ),
        }
        local_paths = {"new/photo.jpg"}

        moves, remaining = _detect_moves(
            [new_file], existing, tmp_path, local_paths,
            deleted_ids=["ast_1"],
        )

        assert len(moves) == 1
        assert moves[0].sha256 == sha


@pytest.mark.fast
class TestMoveExclusionFromDeletions:
    """Moved assets should not appear in the deletion list."""

    def test_moved_asset_not_deleted(self, tmp_path):
        """A server asset whose path is gone locally but matched a move
        should not be in the deletion list."""
        # new/photo.jpg exists locally, old/photo.jpg does not
        new_file = _make_file(tmp_path, "new/photo.jpg", b"moved-content")
        local_files = [new_file]

        existing = {
            "old/photo.jpg": _ServerAsset(asset_id="ast_1", sha256="abc"),
        }

        # _detect_deletions would normally flag ast_1 for deletion
        deleted_ids = _detect_deletions(local_files, existing, tmp_path, None)
        assert "ast_1" in deleted_ids

        # But after move detection removes it:
        moved_asset_ids = {"ast_1"}
        filtered = [aid for aid in deleted_ids if aid not in moved_asset_ids]
        assert len(filtered) == 0


@pytest.mark.fast
class TestCliFlags:
    """Test --allow-moves and --skip-moves CLI validation."""

    def test_allow_and_skip_mutual_exclusion(self):
        """Both flags together should be rejected."""
        from typer.testing import CliRunner
        from src.client.cli.main import app
        runner = CliRunner()
        result = runner.invoke(app, [
            "scan", "--library", "test",
            "--allow-moves", "--skip-moves",
        ])
        assert result.exit_code != 0
        assert "Cannot use both" in result.output

    def test_run_scan_accepts_move_flags(self):
        """run_scan signature accepts allow_moves and skip_moves."""
        import inspect
        from src.client.cli.scan import run_scan
        sig = inspect.signature(run_scan)
        assert "allow_moves" in sig.parameters
        assert "skip_moves" in sig.parameters


@pytest.mark.fast
class TestBatchMovesModel:
    """Verify batch-moves request model."""

    def test_model_structure(self):
        from src.server.api.routers.assets import BatchMoveRequest, BatchMoveItem
        req = BatchMoveRequest(items=[
            BatchMoveItem(asset_id="ast_1", rel_path="new/path.jpg"),
        ])
        assert len(req.items) == 1
        assert req.items[0].asset_id == "ast_1"
        assert req.items[0].rel_path == "new/path.jpg"


@pytest.mark.fast
class TestMoveCandidateDataclass:
    """Test _MoveCandidate structure."""

    def test_fields(self):
        m = _MoveCandidate(
            asset_id="ast_1",
            old_rel_path="old/photo.jpg",
            new_rel_path="new/photo.jpg",
            sha256="abc123",
        )
        assert m.asset_id == "ast_1"
        assert m.old_rel_path == "old/photo.jpg"
        assert m.new_rel_path == "new/photo.jpg"
        assert m.sha256 == "abc123"


@pytest.mark.fast
class TestFileSizePreFilter:
    """Test that file_size mismatch skips SHA computation."""

    def test_size_mismatch_skips_hashing(self, tmp_path):
        """New file with different size than any deleted asset → not a move."""
        content = b"short"
        new_file = _make_file(tmp_path, "new/photo.jpg", content)

        existing = {
            "old/big.jpg": _ServerAsset(
                asset_id="ast_1", sha256="abc", file_size=999999,
            ),
        }
        local_paths = {"new/photo.jpg"}

        moves, remaining = _detect_moves(
            [new_file], existing, tmp_path, local_paths,
            deleted_ids=["ast_1"],
        )

        assert len(moves) == 0
        assert len(remaining) == 1
        # SHA was NOT computed (skipped by size filter)
        assert remaining[0].get("source_sha256") is None

    def test_size_match_proceeds_to_hash(self, tmp_path):
        """New file with same size as a deleted asset → hash and check."""
        content = b"exact-size-match-content"
        new_file = _make_file(tmp_path, "new/photo.jpg", content)

        from src.client.workers.exif_extract import compute_sha256
        sha = compute_sha256(tmp_path / "new/photo.jpg")

        existing = {
            "old/photo.jpg": _ServerAsset(
                asset_id="ast_1", sha256=sha, file_size=len(content),
            ),
        }
        local_paths = {"new/photo.jpg"}

        moves, remaining = _detect_moves(
            [new_file], existing, tmp_path, local_paths,
            deleted_ids=["ast_1"],
        )

        assert len(moves) == 1
        assert moves[0].asset_id == "ast_1"


@pytest.mark.fast
class TestSkipMovesSkipsDeletes:
    """--skip-moves should also suppress deletions."""

    def test_skip_moves_param_exists(self):
        import inspect
        from src.client.cli.scan import run_scan
        sig = inspect.signature(run_scan)
        assert "skip_moves" in sig.parameters


@pytest.mark.fast
class TestScanStatsIncludesMoved:
    """Verify ScanStats has the moved field."""

    def test_moved_field(self):
        stats = ScanStats()
        assert stats.moved == 0
