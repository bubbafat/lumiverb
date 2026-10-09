"""Tests for the scan phase (file discovery, SHA comparison, proxy upload).

These are unit tests that mock the API client. Integration tests that
hit a real database are in test_scan_slow.py (requires Docker).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from src.client.cli.scan import (
    ScanStats,
    _ServerAsset,
    _detect_deletions,
    _fetch_existing_assets_with_sha,
    _populate_cache_for_unchanged,
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


class TestSplitFiles:
    """Test _split_files separates new from existing."""

    def test_new_file(self):
        """File not on server → new."""
        f = {"rel_path": "a.jpg", "media_type": "image"}
        existing: dict[str, _ServerAsset] = {}

        new, needs_hash, fast_unchanged = _split_files([f], existing)
        assert len(new) == 1
        assert len(needs_hash) == 0
        assert len(fast_unchanged) == 0

    def test_existing_file_needs_hash_thorough(self):
        """File on server with thorough=True → needs hash comparison."""
        f = {"rel_path": "a.jpg", "media_type": "image"}
        existing = {"a.jpg": _ServerAsset(asset_id="id-1", sha256="abc")}

        new, needs_hash, fast_unchanged = _split_files([f], existing, thorough=True)
        assert len(new) == 0
        assert len(needs_hash) == 1
        assert needs_hash[0]["_server"].asset_id == "id-1"
        assert len(fast_unchanged) == 0

    def test_existing_file_needs_hash_no_server_mtime(self):
        """File on server without mtime → needs hash (can't fast-skip)."""
        f = {"rel_path": "a.jpg", "media_type": "image", "file_size": 100,
             "file_mtime": datetime(2024, 1, 1, tzinfo=timezone.utc)}
        existing = {"a.jpg": _ServerAsset(asset_id="id-1", sha256="abc", file_size=100)}

        new, needs_hash, fast_unchanged = _split_files([f], existing)
        assert len(needs_hash) == 1
        assert len(fast_unchanged) == 0

    def test_fast_unchanged_mtime_size_match(self):
        """File with matching mtime+size → fast unchanged (no hash needed)."""
        mtime = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        f = {"rel_path": "a.jpg", "media_type": "image", "file_size": 5000,
             "file_mtime": mtime}
        existing = {"a.jpg": _ServerAsset(
            asset_id="id-1", sha256="abc", file_size=5000,
            file_mtime=mtime.isoformat(),
        )}

        new, needs_hash, fast_unchanged = _split_files([f], existing)
        assert len(new) == 0
        assert len(needs_hash) == 0
        assert len(fast_unchanged) == 1
        assert fast_unchanged[0]["asset_id"] == "id-1"

    def test_fast_unchanged_when_the_server_answers_in_its_own_timezone(self):
        """A server whose Postgres runs in, say, America/New_York returns the
        same instant as 08:00-04:00. It's still a match: compare instants,
        not strings, or every scan re-hashes every file."""
        from datetime import timedelta

        mtime = datetime(2024, 6, 15, 12, 0, 0, 123456, tzinfo=timezone.utc)
        f = {"rel_path": "a.jpg", "media_type": "image", "file_size": 5000,
             "file_mtime": mtime}
        new_york = timezone(timedelta(hours=-4))
        existing = {"a.jpg": _ServerAsset(
            asset_id="id-1", sha256="abc", file_size=5000,
            file_mtime=mtime.astimezone(new_york).isoformat(),
        )}

        new, needs_hash, fast_unchanged = _split_files([f], existing)
        assert len(needs_hash) == 0
        assert len(fast_unchanged) == 1

    def test_fast_unchanged_reads_a_z_suffix(self):
        mtime = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        f = {"rel_path": "a.jpg", "media_type": "image", "file_size": 5000,
             "file_mtime": mtime}
        existing = {"a.jpg": _ServerAsset(
            asset_id="id-1", sha256="abc", file_size=5000,
            file_mtime="2024-06-15T12:00:00Z",
        )}

        _, needs_hash, fast_unchanged = _split_files([f], existing)
        assert len(fast_unchanged) == 1

    def test_an_unreadable_server_mtime_needs_hash(self):
        mtime = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        f = {"rel_path": "a.jpg", "media_type": "image", "file_size": 5000,
             "file_mtime": mtime}
        existing = {"a.jpg": _ServerAsset(
            asset_id="id-1", sha256="abc", file_size=5000, file_mtime="yesterday",
        )}

        _, needs_hash, fast_unchanged = _split_files([f], existing)
        assert len(needs_hash) == 1
        assert len(fast_unchanged) == 0

    def test_a_microsecond_apart_needs_hash(self):
        """Same instant only: a file touched since is checked."""
        from datetime import timedelta

        mtime = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        f = {"rel_path": "a.jpg", "media_type": "image", "file_size": 5000,
             "file_mtime": mtime + timedelta(microseconds=1)}
        existing = {"a.jpg": _ServerAsset(
            asset_id="id-1", sha256="abc", file_size=5000, file_mtime=mtime.isoformat(),
        )}

        _, needs_hash, _ = _split_files([f], existing)
        assert len(needs_hash) == 1

    def test_fast_skip_disabled_by_thorough(self):
        """thorough=True forces hash even when mtime+size match."""
        mtime = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        f = {"rel_path": "a.jpg", "media_type": "image", "file_size": 5000,
             "file_mtime": mtime}
        existing = {"a.jpg": _ServerAsset(
            asset_id="id-1", sha256="abc", file_size=5000,
            file_mtime=mtime.isoformat(),
        )}

        new, needs_hash, fast_unchanged = _split_files([f], existing, thorough=True)
        assert len(needs_hash) == 1
        assert len(fast_unchanged) == 0

    def test_size_mismatch_needs_hash(self):
        """Different file size → needs hash (content may have changed)."""
        mtime = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        f = {"rel_path": "a.jpg", "media_type": "image", "file_size": 6000,
             "file_mtime": mtime}
        existing = {"a.jpg": _ServerAsset(
            asset_id="id-1", sha256="abc", file_size=5000,
            file_mtime=mtime.isoformat(),
        )}

        new, needs_hash, fast_unchanged = _split_files([f], existing)
        assert len(needs_hash) == 1
        assert len(fast_unchanged) == 0

    def test_mtime_mismatch_needs_hash(self):
        """Different mtime → needs hash (file may have been modified)."""
        f = {"rel_path": "a.jpg", "media_type": "image", "file_size": 5000,
             "file_mtime": datetime(2024, 7, 1, tzinfo=timezone.utc)}
        existing = {"a.jpg": _ServerAsset(
            asset_id="id-1", sha256="abc", file_size=5000,
            file_mtime=datetime(2024, 6, 15, tzinfo=timezone.utc).isoformat(),
        )}

        new, needs_hash, fast_unchanged = _split_files([f], existing)
        assert len(needs_hash) == 1
        assert len(fast_unchanged) == 0

    def test_mixed(self):
        """Mix of new and existing."""
        files = [
            {"rel_path": "new.jpg", "media_type": "image"},
            {"rel_path": "old.jpg", "media_type": "image"},
        ]
        existing = {"old.jpg": _ServerAsset(asset_id="id-1", sha256="abc")}

        new, needs_hash, fast_unchanged = _split_files(files, existing)
        assert len(new) == 1
        assert new[0]["rel_path"] == "new.jpg"
        assert len(needs_hash) == 1
        assert needs_hash[0]["rel_path"] == "old.jpg"


class TestRunScanAcceptsThorough:
    """Verify run_scan signature accepts thorough parameter."""

    def test_thorough_param_exists(self):
        import inspect
        from src.client.cli.scan import run_scan
        sig = inspect.signature(run_scan)
        assert "thorough" in sig.parameters


class TestDetectDeletions:
    """Test _detect_deletions finds server assets missing from disk."""

    def test_deleted_file(self, tmp_path):
        existing = {"gone.jpg": _ServerAsset(asset_id="id-gone", sha256="abc")}
        deleted = _detect_deletions([], existing, tmp_path, None)
        assert deleted == ["id-gone"]

    def test_no_deletions(self, tmp_path):
        local = [{"rel_path": "a.jpg"}]
        existing = {"a.jpg": _ServerAsset(asset_id="id-1", sha256="abc")}
        deleted = _detect_deletions(local, existing, tmp_path, None)
        assert deleted == []

    def test_path_prefix_scopes_deletion(self, tmp_path):
        """Only assets under the prefix are considered deleted."""
        local = [{"rel_path": "sub/a.jpg"}]
        existing = {
            "sub/a.jpg": _ServerAsset(asset_id="id-1", sha256="x"),
            "other/b.jpg": _ServerAsset(asset_id="id-2", sha256="y"),
            "sub/gone.jpg": _ServerAsset(asset_id="id-3", sha256="z"),
        }
        deleted = _detect_deletions(local, existing, tmp_path, "sub")
        assert set(deleted) == {"id-3"}

    def test_unmounted_root_returns_empty(self):
        """If root doesn't exist, no deletions (safety)."""
        existing = {"a.jpg": _ServerAsset(asset_id="id-1", sha256="x")}
        deleted = _detect_deletions([], existing, Path("/nonexistent"), None)
        assert deleted == []


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
        from src.client.proxy.proxy_cache import ProxyCache
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
        mock_client._client.get.assert_not_called()
        assert stats.cache_populated == 0

    def test_downloads_missing(self, tmp_path):
        """Files not in cache are downloaded from server."""
        from src.client.proxy.proxy_cache import ProxyCache
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


class TestUnchangedFileStat:
    """A file whose size or mtime changed but whose content didn't (touched,
    copied back) is hashed and found unchanged. Its new stat goes to the
    server, or every later scan hashes it again."""

    OLD = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    NEW = datetime(2025, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)

    def _scan(self, tmp_path: Path, *, local_mtime, local_sha="abc", thorough=False,
              client: MagicMock | None = None) -> MagicMock:
        from src.client.cli.scan import run_scan

        root = tmp_path / "lib"
        root.mkdir(exist_ok=True)
        (root / "a.jpg").write_bytes(b"x")
        local = [{"rel_path": "a.jpg", "file_size": 5000, "file_mtime": local_mtime, "media_type": "image",
                  "ext": ".jpg"}]
        existing = {"a.jpg": _ServerAsset(asset_id="ast_1", sha256="abc", file_size=5000,
                                          file_mtime=self.OLD.isoformat(), media_type="image")}
        client = client or MagicMock()
        with (
            patch("src.client.cli.scan._load_tenant_filters", return_value=[]),
            patch("src.client.cli.scan._load_library_filters", return_value=[]),
            patch("src.client.cli.scan._walk_library", return_value=local),
            patch("src.client.cli.scan._fetch_existing_assets_with_sha", return_value=existing),
            patch("src.client.cli.scan._fetch_ignored_paths", return_value={}),
            patch("src.client.cli.scan.compute_sha256", return_value=local_sha),
            patch("src.client.cli.scan._scan_one") as scan_one,
            patch("src.client.cli.scan.ProxyCache"),
            patch("src.client.cli.scan._populate_cache_for_unchanged"),
        ):
            scan_one.return_value = None
            client.stats = run_scan(client, {"library_id": "lib_1", "root_path": str(root)},
                                    console=Console(quiet=True), thorough=thorough)
        client.scan_one = scan_one
        return client

    @staticmethod
    def _stat_posts(client: MagicMock) -> list[dict]:
        return [c.kwargs["json"] for c in client.post.call_args_list if c.args and c.args[0] == "/v1/assets/upsert"]

    def test_a_touched_file_s_new_mtime_goes_to_the_server(self, tmp_path):
        client = self._scan(tmp_path, local_mtime=self.NEW)
        assert self._stat_posts(client) == [{
            "library_id": "lib_1", "rel_path": "a.jpg", "file_size": 5000,
            "file_mtime": self.NEW.isoformat(), "media_type": "image",
        }]
        client.scan_one.assert_not_called()  # unchanged: not re-ingested

    def test_a_changed_file_is_scanned_not_just_restamped(self, tmp_path):
        client = self._scan(tmp_path, local_mtime=self.NEW, local_sha="different")
        assert self._stat_posts(client) == []
        client.scan_one.assert_called_once()

    def test_a_thorough_scan_of_a_matching_file_sends_nothing(self, tmp_path):
        client = self._scan(tmp_path, local_mtime=self.OLD, thorough=True)
        assert self._stat_posts(client) == []

    def test_a_failed_restamp_doesn_t_stop_the_scan(self, tmp_path):
        from src.client.cli.client import LumiverbAPIError

        client = MagicMock()
        client.post.side_effect = LumiverbAPIError("internal", "boom", 500)
        client = self._scan(tmp_path, local_mtime=self.NEW, client=client)
        assert self._stat_posts(client)  # tried; the next scan hashes it again
        assert client.stats.unchanged == 1
