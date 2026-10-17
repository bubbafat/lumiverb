"""A Cache: a Store with a size cap (ADR-020 decision 3).

What a cache holds is a copy, made again when it's gone: past its cap the
least recently used entries go (each read or written is used now), but never
one a local_file block or a hold holds: a tool reading it by its path
(ffmpeg, ffprobe, then ffmpeg again) has it until the block ends, and the
cache is over its cap meanwhile rather than the work failing. Keys are paths
within its folder. A FileCache is a folder on this machine; where the folder
is can change (a previews folder moved), so it's asked for each time. Unlike
the Store's files, nothing here is kept for its own sake: an entry not held
may go between one call and the next, and a reader that already has it open
reads on (the filesystem keeps an open file until it's closed).

`trusted_at_start`: whether what's there is still good when its owner
starts; one that isn't is emptied then (`empty`).
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO

from src.server.storage.files import PARTIAL
from src.server.storage.store import NotFound, Stat, Unavailable, check_key

# A write in progress this old was left by a process that died.
STALE_PARTIAL_SEC = 600

# The files held in this process (local_file, hold), every cache's: never
# evicted meanwhile. A count each, as holds nest.
_held: dict[str, int] = {}
_held_lock = threading.Lock()


def _held_now(path: Path) -> bool:
    with _held_lock:
        return os.path.abspath(path) in _held


class FileCache:
    """A Cache in a folder on this machine."""

    def __init__(self, folder: Callable[[], Path], *, cap_bytes: int, trusted_at_start: bool = True) -> None:
        self._folder = folder
        self.cap_bytes = cap_bytes
        self.trusted_at_start = trusted_at_start

    def _root(self) -> Path:
        try:
            return self._folder()
        except FileNotFoundError as exc:  # its drive isn't plugged in
            raise Unavailable(str(exc)) from exc

    def _path(self, key: str) -> Path:
        root = self._root()
        path = root / check_key(key)
        if not os.path.normpath(path).startswith(os.path.normpath(root) + os.sep):
            raise ValueError(f"Cache key leaves its folder: {key!r}")
        return path

    def _found(self, key: str) -> Path:
        path = self._path(key)
        if not path.is_file():
            raise NotFound(f"Not cached: {key}")
        with contextlib.suppress(OSError):
            os.utime(path)  # used now: the last to go
        return path

    # -- Reading -------------------------------------------------------------------

    def open(self, key: str) -> BinaryIO:
        return open(self._found(key), "rb")

    def read(self, key: str) -> bytes:
        with self.open(key) as f:
            return f.read()

    def size(self, key: str) -> int:
        return self._found(key).stat().st_size

    def stat(self, key: str) -> Stat:
        st = self._found(key).stat()
        return Stat(st.st_size, st.st_mtime)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    @contextlib.contextmanager
    def hold(self, key: str) -> Iterator[None]:
        """The key's file isn't evicted while the block lasts, whether it's
        there yet or not: for a caller that fetches it and then reads it."""
        name = os.path.abspath(self._path(key))
        with _held_lock:
            _held[name] = _held.get(name, 0) + 1
        try:
            yield
        finally:
            with _held_lock:
                if _held[name] > 1:
                    _held[name] -= 1
                else:
                    del _held[name]

    @contextlib.contextmanager
    def local_file(self, key: str) -> Iterator[Path]:
        """The file, held for the block (never evicted meanwhile)."""
        with self.hold(key):
            yield self._found(key)

    # -- Writing -------------------------------------------------------------------

    @contextlib.contextmanager
    def writing(self, key: str, suffix: str = "") -> Iterator[Path]:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f"{PARTIAL}{secrets.token_hex(8)}{suffix}")
        try:
            yield partial
            os.replace(partial, path)
        finally:
            partial.unlink(missing_ok=True)
        self.evict(keep=path)

    def write(self, key: str, data: bytes | BinaryIO) -> None:
        with self.writing(key) as partial, open(partial, "xb") as f:
            if isinstance(data, (bytes, bytearray, memoryview)):
                f.write(data)
            else:
                shutil.copyfileobj(data, f)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def list(self, prefix: str) -> Iterator[str]:
        root = self._root()
        for path in self._files(root):
            key = path.relative_to(root).as_posix()
            if key.startswith(prefix):
                yield key

    # -- The cap -------------------------------------------------------------------

    @staticmethod
    def _files(root: Path) -> Iterator[Path]:
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                if not name.startswith(PARTIAL):
                    yield Path(dirpath, name)

    def evict(self, *, keep: Path | None = None, dry_run: bool = False) -> int:
        """The least recently used entries go until what's left fits the cap
        (never `keep`, the entry just written, nor one held), and writes in
        progress a dead process left. How many went (dry_run: would go,
        nothing removed)."""
        root = self._root()
        entries = []
        gone = 0
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                path = Path(dirpath, name)
                try:
                    st = path.stat()
                except OSError:
                    continue
                if name.startswith(PARTIAL):
                    if st.st_mtime < _now() - STALE_PARTIAL_SEC:
                        if not dry_run:
                            path.unlink(missing_ok=True)
                        gone += 1
                    continue
                entries.append((st.st_mtime, st.st_size, path))
        total = sum(size for _, size, _ in entries)
        for _, size, path in sorted(entries):
            if total <= self.cap_bytes:
                break
            if path == keep or _held_now(path):
                continue
            if not dry_run:
                path.unlink(missing_ok=True)
            total -= size
            gone += 1
        return gone

    def empty(self) -> None:
        """Everything goes: what's there isn't trusted any more."""
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(self._root())


# The kind playback cuts are kept under, in an account's folder of the previews folder.
PLAYBACK = "playback"


def playback_cuts(tenant_id: str, root: Path | None = None) -> FileCache:
    """An account's playback cuts (src/server/api/routers/playback.py): the
    start of each video it plays within a cap, made again on demand. In
    `{account}/playback` of the previews folder (root, else the install's,
    wherever it is now), at most LUMIVERB_PLAYBACK_CACHE_GB."""
    from src.server.config import get_settings
    from src.server.storage.local import get_storage
    from src.server.storage.store import check_key

    check_key(tenant_id)
    cap = int(get_settings().lumiverb_playback_cache_gb * 1024**3)
    if root is not None:
        return FileCache(lambda: root / tenant_id / PLAYBACK, cap_bytes=cap)
    storage = get_storage()
    return FileCache(lambda: storage.write_path(f"{tenant_id}/{PLAYBACK}"), cap_bytes=cap)


def _now() -> float:
    import time

    return time.time()
