"""Local filesystem storage for the stored artifacts (the previews)."""

import errno
import logging
import os
import stat
import threading
from functools import lru_cache
from pathlib import Path
from typing import cast

from src.server.config import get_settings
from src.server.storage import store
from src.server.storage.store import UnsafeKeyError
from src.shared import previews_location

__all__ = ["LocalStorage", "UnsafeKeyError", "get_storage", "through_a_link"]

logger = logging.getLogger(__name__)


def through_a_link(root: Path | str, path: Path | str) -> bool:
    """Whether `path` (in `root`) leaves it, or a part of it below `root` is a
    symbolic link, as the disk is now (a part not there yet is none). What
    Lumiverb reads, writes, lists, moves and deletes in a previews folder is
    never through one, whatever it points at: a link is refused (read,
    written) or passed by (listed, moved, swept, deleted). The root itself
    may be one: it's the folder an admin chose."""
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    if rel == os.curdir:
        return False
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return True
    at = os.path.abspath(root)
    for part in rel.split(os.sep):
        at = os.path.join(at, part)
        try:
            if stat.S_ISLNK(os.lstat(at).st_mode):
                return True
        except OSError:
            return False  # nothing further is there yet (or it can't be looked at: neither can it be opened)
    return False


class LocalStorage:
    """
    Local filesystem storage. Keys are relative to the previews folder:
    DATA_DIR, or the one an admin chose, or the key's library's own
    (src/shared/previews_location.py).

    Nothing is read, written or deleted through a symbolic link under any
    of those folders (through_a_link).

    Path structure:
      {previews}/{tenant_id}/{library_id}/proxies/{bucket}/{asset_id}_{filename}.webp
      {previews}/{tenant_id}/{library_id}/thumbnails/{bucket}/{asset_id}_{filename}.webp
      {previews}/{tenant_id}/{library_id}/analysis/{bucket}/{asset_id}_{filename}.mp4

    bucket = int(asset_id_ulid_timestamp) % 100, zero-padded to 2 digits.

    While the previews move (src/server/storage/move.py), a key is read where
    it is now (moved or not yet), written to the new folder, and deleted in
    both. Where they are is read from storage.json when it changes, so every
    API worker follows a move as it starts and ends.
    """

    def __init__(self, data_dir: str | None = None) -> None:
        self._data_dir = data_dir if data_dir is not None else get_settings().data_dir
        self._lock = threading.Lock()
        self._seen: tuple[int, int] | None = None
        self._location = previews_location.read(self._data_dir)

    @property
    def location(self) -> previews_location.Location:
        """Where the previews are now (and, during a move, were)."""
        try:
            st = os.stat(Path(self._data_dir) / previews_location.FILE)
            seen = (st.st_mtime_ns, st.st_ino)
        except OSError:
            seen = None
        with self._lock:
            if seen != self._seen:
                self._seen = seen
                self._location = previews_location.read(self._data_dir)
            return self._location

    @property
    def root(self) -> Path:
        """Where new files go, but a library's with a folder of its own."""
        return self.location.previews

    # The key builders are the Store's (src/server/storage/store.py); these
    # stay for the callers that build keys through a LocalStorage until they
    # use the Store (ADR-020 phase 2).
    proxy_key = staticmethod(store.proxy_key)
    thumbnail_key = staticmethod(store.thumbnail_key)
    scene_rep_key = staticmethod(store.scene_rep_key)
    video_preview_key = staticmethod(store.video_preview_key)
    analysis_proxy_key = staticmethod(store.analysis_proxy_key)
    face_crop_key = staticmethod(store.face_crop_key)
    face_crop_key_of = staticmethod(store.face_crop_key_of)

    @staticmethod
    def path_in(root: Path, key: str, *, passing_links: bool = False) -> Path | None:
        """The key's path in `root`. A key that would leave it is refused
        (UnsafeKeyError): an absolute key, one with a `..` part, or one that
        resolves outside it; so is one through a symbolic link there
        (through_a_link), or with passing_links it's None, and logged."""
        store.check_key(key)
        base = os.path.abspath(root)
        path = Path(base) / key
        if not os.path.normpath(path).startswith(base.rstrip(os.sep) + os.sep):
            raise UnsafeKeyError(f"Storage key leaves the previews folder: {key!r}")
        if through_a_link(base, path):
            if passing_links:
                logger.warning("Passed by %s in %s: it's through a symbolic link, never followed there", key, base)
                return None
            raise UnsafeKeyError(f"Storage key goes through a symbolic link in the previews folder {base}: {key!r}")
        return path

    @classmethod
    def _under(cls, root: Path, key: str) -> Path:
        """The key's path in `root`; one through a link refused, as path_in does."""
        return cast(Path, cls.path_in(root, key))  # never None: it refuses instead

    def paths(self, key: str) -> list[Path]:
        """Every place the key may be: its folder, then (moving) the old one."""
        location = self.location
        return [self._under(root, key) for root in location.roots_for(location.library_of(key))]

    def abs_path(self, key: str) -> Path:
        """Where the key is to be read: where it is (during a move, moved or
        not yet), else where it goes. Writes go through write_path."""
        candidates = self.paths(key)
        for path in candidates[1:]:
            if not candidates[0].exists() and path.exists():
                return path
        return candidates[0]

    def write_path(self, key: str) -> Path:
        """Where the key is written: the previews folder, always. Raises
        FileNotFoundError when that's a chosen folder whose drive isn't
        plugged in: its empty mount point is on the system disk."""
        location = self.location
        root = location.root_for(location.library_of(key))
        if not location.available(self._data_dir, root):
            raise FileNotFoundError(errno.ENOENT, "The previews folder isn't there. Is its drive plugged in?",
                                    str(root))
        return self._under(root, key)

    def write(self, key: str, data: bytes) -> None:
        path = self.write_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_bytes(data)
        tmp_path.rename(path)

    def exists(self, key: str) -> bool:
        return any(path.exists() for path in self.paths(key))

    def delete(self, key: str) -> bool:
        """Removes the key wherever it is (never through a link); True if a file went."""
        location = self.location
        removed = False
        for root in location.roots_for(location.library_of(key)):
            if (path := self.path_in(root, key, passing_links=True)) is None:
                continue
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                pass
        return removed


@lru_cache(maxsize=1)
def get_storage() -> LocalStorage:
    """Return a cached LocalStorage instance using settings.data_dir."""
    return LocalStorage()
