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
