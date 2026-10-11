"""Local filesystem storage for the stored artifacts (the previews)."""

import errno
import os
import threading
from functools import lru_cache
from pathlib import Path, PurePosixPath

from ulid import ULID

from src.server.config import get_settings
from src.shared import previews_location


class UnsafeKeyError(ValueError):
    """A storage key that would reach outside the previews folder."""


class LocalStorage:
    """
    Local filesystem storage. Keys are relative to the previews folder:
    DATA_DIR, or the one an admin chose, or the key's library's own
    (src/shared/previews_location.py).

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

    @staticmethod
    def _bucket_from_asset_id(asset_id: str) -> int:
        """Extract ULID from asset_id (strip ast_ prefix) and return bucket 0-99."""
        ulid_str = asset_id.removeprefix("ast_")
        ulid = ULID.from_str(ulid_str)
        return int(ulid) % 100

    def proxy_key(
        self, tenant_id: str, library_id: str, asset_id: str, original_filename: str
    ) -> str:
        bucket = self._bucket_from_asset_id(asset_id)
        original_stem = Path(original_filename).stem
        return f"{tenant_id}/{library_id}/proxies/{bucket:02d}/{asset_id}_{original_stem}.webp"

    def thumbnail_key(
        self, tenant_id: str, library_id: str, asset_id: str, original_filename: str
    ) -> str:
        bucket = self._bucket_from_asset_id(asset_id)
        original_stem = Path(original_filename).stem
        return f"{tenant_id}/{library_id}/thumbnails/{bucket:02d}/{asset_id}_{original_stem}.webp"

    def scene_rep_key(
        self, tenant_id: str, library_id: str, asset_id: str, rep_frame_ms: int
    ) -> str:
        bucket = self._bucket_from_asset_id(asset_id)
        return (
            f"{tenant_id}/{library_id}/scenes/{bucket:02d}"
            f"/{asset_id}_{rep_frame_ms:010d}.jpg"
        )

    def video_preview_key(
        self, tenant_id: str, library_id: str, asset_id: str, rel_path: str
    ) -> str:
        """
        Return the storage key for a generated MP4 video preview.

        Uses the same bucketing scheme as proxies/thumbnails and preserves the
        original stem from the relative path, but always uses `.mp4` as the
        extension for the preview clip.
        """
        bucket = self._bucket_from_asset_id(asset_id)
        original_stem = Path(rel_path).stem
        return (
            f"{tenant_id}/{library_id}/previews/{bucket:02d}/"
            f"{asset_id}_{original_stem}.mp4"
        )

    def analysis_proxy_key(
        self, tenant_id: str, library_id: str, asset_id: str, rel_path: str
    ) -> str:
        """Storage key for a video's full-length analysis proxy (MP4)."""
        bucket = self._bucket_from_asset_id(asset_id)
        original_stem = Path(rel_path).stem
        return (
            f"{tenant_id}/{library_id}/analysis/{bucket:02d}/"
            f"{asset_id}_{original_stem}.mp4"
        )

    @staticmethod
    def _bucket_from_face_id(face_id: str) -> int:
        """Extract ULID from face_id (strip face_ prefix) and return bucket 0-99."""
        ulid_str = face_id.removeprefix("face_")
        ulid = ULID.from_str(ulid_str)
        return int(ulid) % 100

    def face_crop_key(
        self, tenant_id: str, library_id: str, face_id: str
    ) -> str:
        bucket = self._bucket_from_face_id(face_id)
        return f"{tenant_id}/{library_id}/face_crops/{bucket:02d}/{face_id}.webp"

    @staticmethod
    def _under(root: Path, key: str) -> Path:
        """The key's path in `root`. A key that would leave it is refused: an
        absolute key, one with a `..` part, or one that resolves outside it."""
        if not isinstance(key, str) or not key or "\x00" in key:
            raise UnsafeKeyError(f"Invalid storage key: {key!r}")
        rel = PurePosixPath(key)
        if rel.is_absolute() or key.startswith("\\") or ".." in rel.parts:
            raise UnsafeKeyError(f"Storage key leaves the previews folder: {key!r}")
        base = os.path.abspath(root)
        path = Path(base) / key
        # Checked on the path itself, not through links: a link inside the
        # folder (an operator's choice) is followed as before.
        if not os.path.normpath(path).startswith(base.rstrip(os.sep) + os.sep):
            raise UnsafeKeyError(f"Storage key leaves the previews folder: {key!r}")
        return path

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
        """Removes the key wherever it is; True if a file went."""
        removed = False
        for path in self.paths(key):
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
