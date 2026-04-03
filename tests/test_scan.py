"""Tests for the scan phase (file discovery, SHA comparison, proxy upload).

These are unit tests that mock the API client. Integration tests that
hit a real database are in test_scan_slow.py (requires Docker).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from src.cli.scan import (
    ScanStats,
    _ServerAsset,
    _classify_files,
    _fetch_existing_assets_with_sha,
    _populate_cache_for_unchanged,
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


class TestClassifyFiles:
    """Test _classify_files change detection logic."""

    def test_new_file(self, tmp_path):
        """File on disk not on server → new."""
        f = _make_file(tmp_path, "a.jpg")
        existing: dict[str, _ServerAsset] = {}
        console = Console(quiet=True)

        new, changed, unchanged, deleted = _classify_files(
            [f], existing, tmp_path, None, False, console,
        )
        assert len(new) == 1
        assert new[0]["rel_path"] == "a.jpg"
        assert new[0]["source_sha256"] is not None
        assert len(changed) == 0
        assert len(unchanged) == 0
        assert len(deleted) == 0

    def test_unchanged_file(self, tmp_path):
        """File on disk with matching SHA → unchanged."""
        content = b"stable-content"
        f = _make_file(tmp_path, "a.jpg", content)

        # Compute the real SHA
        from src.workers.exif_extract import compute_sha256
        real_sha = compute_sha256(tmp_path / "a.jpg")

        existing = {"a.jpg": _ServerAsset(asset_id="id-1", sha256=real_sha)}
        console = Console(quiet=True)

        new, changed, unchanged, deleted = _classify_files(
            [f], existing, tmp_path, None, False, console,
        )
        assert len(new) == 0
        assert len(changed) == 0
        assert len(unchanged) == 1
        assert unchanged[0]["asset_id"] == "id-1"

    def test_changed_file(self, tmp_path):
        """File on disk with different SHA → changed."""
        f = _make_file(tmp_path, "a.jpg", b"new-content")
        existing = {"a.jpg": _ServerAsset(asset_id="id-1", sha256="old-sha-that-wont-match")}
        console = Console(quiet=True)

        new, changed, unchanged, deleted = _classify_files(
            [f], existing, tmp_path, None, False, console,
        )
        assert len(new) == 0
        assert len(changed) == 1
        assert changed[0]["asset_id"] == "id-1"

    def test_deleted_file(self, tmp_path):
        """Asset on server but file missing from disk → deleted."""
        existing = {"gone.jpg": _ServerAsset(asset_id="id-gone", sha256="abc")}
        console = Console(quiet=True)

        new, changed, unchanged, deleted = _classify_files(
            [], existing, tmp_path, None, False, console,
        )
        assert len(deleted) == 1
        assert deleted[0] == "id-gone"

    def test_force_treats_unchanged_as_changed(self, tmp_path):
        """--force flag causes unchanged files to be classified as changed."""
        content = b"stable-content"
        f = _make_file(tmp_path, "a.jpg", content)

        from src.workers.exif_extract import compute_sha256
        real_sha = compute_sha256(tmp_path / "a.jpg")

        existing = {"a.jpg": _ServerAsset(asset_id="id-1", sha256=real_sha)}
        console = Console(quiet=True)

        new, changed, unchanged, deleted = _classify_files(
            [f], existing, tmp_path, None, True, console,
        )
        assert len(unchanged) == 0
        assert len(changed) == 1
        assert changed[0]["asset_id"] == "id-1"

    def test_path_prefix_scopes_deletion(self, tmp_path):
        """Deletion detection only affects assets under the path prefix."""
        f = _make_file(tmp_path, "sub/a.jpg")
        existing = {
            "sub/a.jpg": _ServerAsset(asset_id="id-1", sha256="x"),
            "other/b.jpg": _ServerAsset(asset_id="id-2", sha256="y"),
            "sub/gone.jpg": _ServerAsset(asset_id="id-3", sha256="z"),
        }
        console = Console(quiet=True)

        _, _, _, deleted = _classify_files(
            [f], existing, tmp_path, "sub", False, console,
        )
        # Only sub/gone.jpg should be deleted, not other/b.jpg
        assert set(deleted) == {"id-3"}

    def test_mixed_state(self, tmp_path):
        """Multiple files in different states."""
        new_f = _make_file(tmp_path, "new.jpg", b"new")
        changed_f = _make_file(tmp_path, "changed.jpg", b"changed")
        unchanged_content = b"unchanged"
        unchanged_f = _make_file(tmp_path, "unchanged.jpg", unchanged_content)

        from src.workers.exif_extract import compute_sha256
        unchanged_sha = compute_sha256(tmp_path / "unchanged.jpg")

        existing = {
            "changed.jpg": _ServerAsset(asset_id="id-c", sha256="old"),
            "unchanged.jpg": _ServerAsset(asset_id="id-u", sha256=unchanged_sha),
            "deleted.jpg": _ServerAsset(asset_id="id-d", sha256="x"),
        }
        console = Console(quiet=True)

        new, changed, unchanged, deleted = _classify_files(
            [new_f, changed_f, unchanged_f], existing, tmp_path, None, False, console,
        )
        assert len(new) == 1
        assert len(changed) == 1
        assert len(unchanged) == 1
        assert len(deleted) == 1


class TestFetchExistingAssetsWithSha:
    """Test server asset paging with SHA extraction."""

    def test_single_page(self):
        mock_client = MagicMock()
        mock_client.get.return_value.json.return_value = {
            "items": [
                {"rel_path": "a.jpg", "asset_id": "id-a", "sha256": "sha-a"},
                {"rel_path": "b.jpg", "asset_id": "id-b", "sha256": None},
            ],
            "next_cursor": None,
        }

        result = _fetch_existing_assets_with_sha(mock_client, "lib-1")
        assert len(result) == 2
        assert result["a.jpg"].asset_id == "id-a"
        assert result["a.jpg"].sha256 == "sha-a"
        assert result["b.jpg"].sha256 is None

    def test_multiple_pages(self):
        mock_client = MagicMock()
        mock_client.get.return_value.json.side_effect = [
            {
                "items": [{"rel_path": "a.jpg", "asset_id": "id-a", "sha256": "sha-a"}],
                "next_cursor": "cursor-1",
            },
            {
                "items": [{"rel_path": "b.jpg", "asset_id": "id-b", "sha256": "sha-b"}],
                "next_cursor": None,
            },
        ]

        result = _fetch_existing_assets_with_sha(mock_client, "lib-1")
        assert len(result) == 2


class TestPopulateCacheForUnchanged:
    """Test proxy download for unchanged files with missing cache."""

    def test_skips_cached(self, tmp_path):
        """Files already in cache are skipped."""
        from src.cli.proxy_cache import ProxyCache
        cache = ProxyCache()
        cache._dir = tmp_path
        cache.put_scan("id-1", b"proxy", "sha")

        stats = ScanStats()
        console = Console(quiet=True)
        mock_client = MagicMock()

        _populate_cache_for_unchanged(
            mock_client,
            [{"asset_id": "id-1", "source_sha256": "sha"}],
            cache, stats, console,
        )
        # Should not have attempted any download
        mock_client._client.get.assert_not_called()
        assert stats.cache_populated == 0

    def test_downloads_missing(self, tmp_path):
        """Files not in cache are downloaded from server."""
        from src.cli.proxy_cache import ProxyCache
        cache = ProxyCache()
        cache._dir = tmp_path

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"server-proxy-bytes"

        mock_client = MagicMock()
        mock_client._client.get.return_value = mock_resp

        stats = ScanStats()
        console = Console(quiet=True)

        _populate_cache_for_unchanged(
            mock_client,
            [{"asset_id": "id-1", "source_sha256": "sha-1"}],
            cache, stats, console,
        )
        assert stats.cache_populated == 1
        assert (tmp_path / "id-1").read_bytes() == b"server-proxy-bytes"
        assert (tmp_path / "id-1.sha").read_text() == "sha-1"
