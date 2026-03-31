"""Tests for the disk-backed proxy cache."""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from src.cli.proxy_cache import ProxyCache, _prune_stale_caches, _CACHE_PREFIX


def test_put_get_remove():
    cache = ProxyCache()
    try:
        assert cache.get("asset_1") is None

        cache.put("asset_1", b"jpeg-bytes-1")
        assert cache.get("asset_1") == b"jpeg-bytes-1"

        cache.put("asset_2", b"jpeg-bytes-2")
        assert cache.get("asset_2") == b"jpeg-bytes-2"

        cache.remove("asset_1")
        assert cache.get("asset_1") is None
        assert cache.get("asset_2") == b"jpeg-bytes-2"
    finally:
        cache.cleanup()


def test_cleanup_removes_directory():
    cache = ProxyCache()
    cache_dir = cache.path
    cache.put("asset_1", b"data")
    assert cache_dir.exists()

    cache.cleanup()
    assert not cache_dir.exists()


def test_cleanup_idempotent():
    cache = ProxyCache()
    cache.cleanup()
    cache.cleanup()  # should not raise


def test_remove_missing_key():
    cache = ProxyCache()
    try:
        cache.remove("nonexistent")  # should not raise
    finally:
        cache.cleanup()


def test_prune_stale_caches():
    """Stale cache dirs (dead PID) are pruned on startup."""
    tmp = Path(tempfile.gettempdir())
    # Create a fake stale cache with a PID that doesn't exist
    stale_dir = tmp / f"{_CACHE_PREFIX}999999999-fakesuffix"
    stale_dir.mkdir(exist_ok=True)
    (stale_dir / "asset_1").write_bytes(b"stale")

    try:
        _prune_stale_caches()
        assert not stale_dir.exists()
    finally:
        if stale_dir.exists():
            shutil.rmtree(stale_dir)


def test_prune_preserves_live_cache():
    """Cache dir for current (live) PID is not pruned."""
    cache = ProxyCache()
    try:
        cache.put("asset_1", b"data")
        _prune_stale_caches()
        assert cache.get("asset_1") == b"data"
    finally:
        cache.cleanup()


def test_cache_dir_contains_pid():
    cache = ProxyCache()
    try:
        assert str(os.getpid()) in cache.path.name
    finally:
        cache.cleanup()
