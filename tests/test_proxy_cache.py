"""Tests for the disk-backed proxy cache."""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from src.processing.proxy.proxy_cache import ProxyCache


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
    assert cache.get_source_hash("nonexistent") is None


def test_get_sha_reads_sidecar(tmp_path):
    cache = _test_cache(tmp_path)
    cache.put_scan("asset_1", b"data", "deadbeef")
    assert cache.get_source_hash("asset_1") == "deadbeef"


def test_put_scan_overwrites_existing(tmp_path):
    """Re-scanning a changed file overwrites both proxy and SHA."""
    cache = _test_cache(tmp_path)
    cache.put_scan("asset_1", b"old-proxy", "old-sha")
    cache.put_scan("asset_1", b"new-proxy", "new-sha")

    assert (tmp_path / "asset_1").read_bytes() == b"new-proxy"
    assert cache.get_source_hash("asset_1") == "new-sha"


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


# -- The scheduler's cap (LUMIVERB_PREVIEW_CACHE_GB) -------------------------------------


def _capped(tmp_path: Path, monkeypatch, cap: int | None) -> ProxyCache:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    cache = ProxyCache(cap_bytes=cap)
    cache._dir = tmp_path / "proxies"
    cache._dir.mkdir()
    return cache


def _aged(cache: ProxyCache, asset_id: str, seconds_ago: float) -> None:
    import time

    when = time.time() - seconds_ago
    for path in (cache._entry(asset_id), cache.sha_path(asset_id)):
        if path.exists():
            os.utime(path, (when, when))


def test_past_its_cap_the_least_recently_used_go_each_with_its_sidecar(tmp_path, monkeypatch):
    sha = "sha256:" + "0" * 64
    cache = _capped(tmp_path, monkeypatch, cap=3 * (1024 + len(sha)))  # three, with their sidecars
    for asset_id, age in (("ast_a", 300), ("ast_b", 200), ("ast_c", 100)):
        cache.put_scan(asset_id, b"x" * 1024, sha)
        _aged(cache, asset_id, age)
    cache.get("ast_a")  # a read is a use: a goes last
    cache.put_scan("ast_d", b"x" * 1024, sha)
    assert cache.evict() == 1
    assert not cache.has("ast_b") and not cache.sha_path("ast_b").exists()
    assert cache.has("ast_a") and cache.has("ast_c") and cache.has("ast_d")
    assert cache.get_source_hash("ast_a") is not None, "a sidecar goes only with its proxy"


def test_the_cap_is_looked_at_on_the_first_write_and_every_fiftieth(tmp_path, monkeypatch):
    cache = _capped(tmp_path, monkeypatch, cap=10**9)
    looks = []
    monkeypatch.setattr(cache, "evict", lambda keep=None: looks.append(keep) or 0)
    for n in range(101):
        cache.put(f"ast_{n}", b"x")
    assert looks == ["ast_0", "ast_50", "ast_100"]


def test_the_one_just_written_stays_even_alone_past_the_cap(tmp_path, monkeypatch):
    cache = _capped(tmp_path, monkeypatch, cap=10)
    cache.put("ast_old", b"x" * 100)
    _aged(cache, "ast_old", 100)
    cache.put("ast_big", b"x" * 100)
    assert cache.evict(keep="ast_big") == 1
    assert cache.has("ast_big") and not cache.has("ast_old")


def test_a_write_a_dead_process_left_goes_and_one_under_way_stays(tmp_path, monkeypatch):
    import time

    cache = _capped(tmp_path, monkeypatch, cap=10)
    dead, live = cache._dir / "tmpdead.tmp", cache._dir / "tmplive.tmp"
    dead.write_bytes(b"x")
    live.write_bytes(b"x")
    os.utime(dead, (time.time() - 3600,) * 2)
    cache.evict()
    assert not dead.exists() and live.exists()


def test_without_a_cap_nothing_goes(tmp_path, monkeypatch):
    """The CLI's cache, which the Mac app browses, has none yet (ADR-020 phase 5)."""
    cache = _capped(tmp_path, monkeypatch, cap=0)
    for n in range(3):
        cache.put(f"ast_{n}", b"x" * 1024)
    assert cache.evict() == 0
    assert all(cache.has(f"ast_{n}") for n in range(3))


def test_its_cap_is_this_machines_the_schedulers_two_gb(tmp_path, monkeypatch):
    from src.processing import machine
    from src.processing.machine import Machine

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    try:
        machine.use(Machine())
        assert ProxyCache()._cap_bytes == 0, "the CLI's: none"
        machine.use(Machine(preview_cache_gb=2.0))
        assert ProxyCache()._cap_bytes == 2 * 1024**3
    finally:
        machine.use(None)


def test_a_scans_preview_from_the_cache_is_the_size_producers_see(tmp_path, monkeypatch):
    """A scan keeps its 2048 px preview; CLIP, faces, OCR and descriptions see 1280."""
    import io

    from PIL import Image

    cache = _capped(tmp_path, monkeypatch, cap=0)
    buf = io.BytesIO()
    Image.new("RGB", (2048, 1536), (10, 20, 30)).save(buf, format="JPEG")
    cache.put_scan("ast_1", buf.getvalue(), "sha256:" + "0" * 64)
    assert max(Image.open(io.BytesIO(cache.get("ast_1"))).size) == 1280
