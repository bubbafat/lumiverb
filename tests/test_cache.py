"""A Cache's policy (src/server/storage/cache.py, ADR-020 decision 3): past its
cap the least recently used entries go, never the one just written; a write
a dead process left goes too; an untrusted cache is emptied. What it keeps
and reads is the Store's contract (tests/test_store_contract.py)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from src.server.storage.cache import STALE_PARTIAL_SEC, FileCache, playback_cuts
from src.server.storage.files import PARTIAL

pytestmark = pytest.mark.fast

KB = 1024


def _aged(cache: FileCache, key: str, seconds_ago: float) -> None:
    with cache.local_file(key) as path:
        when = time.time() - seconds_ago
        os.utime(path, (when, when))


def test_past_its_cap_the_least_recently_used_go_first(tmp_path):
    cache = FileCache(lambda: tmp_path, cap_bytes=3 * KB)
    for name, age in (("a", 300), ("b", 200), ("c", 100)):
        cache.write(name, b"x" * KB)
        _aged(cache, name, age)
    cache.read("a")  # used now: a goes last
    cache.write("d", b"x" * KB)  # 4 KB: one has to go
    assert sorted(cache.list("")) == ["a", "c", "d"]


def test_the_entry_just_written_stays_even_alone_past_the_cap(tmp_path):
    cache = FileCache(lambda: tmp_path, cap_bytes=KB)
    cache.write("old", b"x" * KB)
    cache.write("big", b"x" * 5 * KB)
    assert list(cache.list("")) == ["big"]


def test_a_write_a_dead_process_left_goes_and_one_under_way_stays(tmp_path):
    cache = FileCache(lambda: tmp_path, cap_bytes=10 * KB)
    (tmp_path / f"{PARTIAL}dead.mp4").write_bytes(b"half")
    (tmp_path / f"{PARTIAL}live.mp4").write_bytes(b"half")
    old = time.time() - STALE_PARTIAL_SEC - 60
    os.utime(tmp_path / f"{PARTIAL}dead.mp4", (old, old))
    assert cache.evict(dry_run=True) == 1 and (tmp_path / f"{PARTIAL}dead.mp4").exists()
    assert cache.evict() == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{PARTIAL}live.mp4"]
    assert list(cache.list("")) == [], "a write in progress is never an entry"


def test_an_untrusted_cache_is_emptied(tmp_path):
    cache = FileCache(lambda: tmp_path / "c", cap_bytes=KB, trusted_at_start=False)
    cache.write("a/b", b"x")
    cache.empty()
    cache.empty()  # nothing there: fine
    assert list(cache.list("")) == []


def test_a_key_never_leaves_its_folder(tmp_path):
    cache = FileCache(lambda: tmp_path / "c", cap_bytes=KB)
    (tmp_path / "secret").write_bytes(b"s")
    for key in ("../secret", "/etc/passwd", "a/../../secret"):
        with pytest.raises(ValueError):
            cache.read(key)
    assert (tmp_path / "secret").read_bytes() == b"s"


def test_an_accounts_playback_cuts_are_in_its_folder_of_the_previews_folder(tmp_path: Path):
    cuts = playback_cuts("ten_1", root=tmp_path)
    cuts.write("ast_1_preview_v_10s.mp4", b"cut")
    assert (tmp_path / "ten_1" / "playback" / "ast_1_preview_v_10s.mp4").read_bytes() == b"cut"
    with pytest.raises(ValueError):
        playback_cuts("../ten_1", root=tmp_path)
