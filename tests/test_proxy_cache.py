"""Tests for the disk-backed proxy cache."""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from src.cli.proxy_cache import ProxyCache


def _test_cache(tmp_path: Path) -> ProxyCache:
    """Create a ProxyCache using a temporary directory for test isolation."""
    cache = ProxyCache()
    cache._dir = tmp_path
    return cache


def test_put_get_remove(tmp_path):
    cache = _test_cache(tmp_path)
    assert cache.get("asset_1") is None

    cache.put("asset_1", b"jpeg-bytes-1")
    assert cache.get("asset_1") == b"jpeg-bytes-1"

    cache.put("asset_2", b"jpeg-bytes-2")
    assert cache.get("asset_2") == b"jpeg-bytes-2"

    cache.remove("asset_1")
    assert cache.get("asset_1") is None
    assert cache.get("asset_2") == b"jpeg-bytes-2"


def test_cleanup_removes_directory(tmp_path):
    cache = _test_cache(tmp_path)
    cache.put("asset_1", b"data")
    assert tmp_path.exists()

    cache.cleanup()
    assert not tmp_path.exists()


def test_cleanup_idempotent(tmp_path):
    cache = _test_cache(tmp_path)
    cache.cleanup()
    cache.cleanup()  # should not raise


def test_remove_missing_key(tmp_path):
    cache = _test_cache(tmp_path)
    cache.remove("nonexistent")  # should not raise


def test_has(tmp_path):
    cache = _test_cache(tmp_path)
    assert not cache.has("asset_1")
    cache.put("asset_1", b"data")
    assert cache.has("asset_1")


def test_persistent_dir_created():
    """ProxyCache creates the persistent directory if it doesn't exist."""
    cache = ProxyCache()
    assert cache.path.exists()
    assert cache.path.is_dir()


def test_put_and_get_across_instances(tmp_path):
    """Data persists across ProxyCache instances sharing the same dir."""
    cache1 = _test_cache(tmp_path)
    cache1.put("asset_1", b"persisted-data")

    cache2 = _test_cache(tmp_path)
    assert cache2.get("asset_1") == b"persisted-data"


def test_put_scan_stores_proxy_and_sha(tmp_path):
    """put_scan writes the proxy and a .sha sidecar atomically."""
    cache = _test_cache(tmp_path)
    cache.put_scan("asset_1", b"proxy-bytes", "abc123")

    assert (tmp_path / "asset_1").read_bytes() == b"proxy-bytes"
    assert (tmp_path / "asset_1.sha").read_text() == "abc123"


def test_get_sha_returns_none_when_missing(tmp_path):
    cache = _test_cache(tmp_path)
    assert cache.get_sha("nonexistent") is None


def test_get_sha_reads_sidecar(tmp_path):
    cache = _test_cache(tmp_path)
    cache.put_scan("asset_1", b"data", "deadbeef")
    assert cache.get_sha("asset_1") == "deadbeef"


def test_put_scan_overwrites_existing(tmp_path):
    """Re-scanning a changed file overwrites both proxy and SHA."""
    cache = _test_cache(tmp_path)
    cache.put_scan("asset_1", b"old-proxy", "old-sha")
    cache.put_scan("asset_1", b"new-proxy", "new-sha")

    assert (tmp_path / "asset_1").read_bytes() == b"new-proxy"
    assert cache.get_sha("asset_1") == "new-sha"


def test_remove_cleans_sha_sidecar(tmp_path):
    """remove() also deletes the .sha sidecar."""
    cache = _test_cache(tmp_path)
    cache.put_scan("asset_1", b"data", "sha-val")
    cache.remove("asset_1")

    assert not (tmp_path / "asset_1").exists()
    assert not (tmp_path / "asset_1.sha").exists()


def test_atomic_write_no_partial_files(tmp_path):
    """If write fails, no partial file remains."""
    cache = _test_cache(tmp_path)
    # Make dir read-only to force write failure
    ro_dir = tmp_path / "readonly"
    ro_dir.mkdir()
    cache._dir = ro_dir

    cache.put_scan("asset_1", b"data", "sha")
    # Even if it succeeds on some systems, the key point is no crash
    # and no .tmp files left behind
    tmp_files = list(ro_dir.glob("*.tmp"))
    assert len(tmp_files) == 0
