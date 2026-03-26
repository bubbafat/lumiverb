"""Local filesystem storage for proxy and thumbnail files."""

from functools import lru_cache
from pathlib import Path

from ulid import ULID

from src.core.config import get_settings


class LocalStorage:
    """
    Local filesystem storage. All paths are relative to DATA_DIR.

    Path structure:
      {data_dir}/{tenant_id}/{library_id}/proxies/{bucket}/{asset_id}_{filename}.webp
      {data_dir}/{tenant_id}/{library_id}/thumbnails/{bucket}/{asset_id}_{filename}.webp

    bucket = int(asset_id_ulid_timestamp) % 100, zero-padded to 2 digits.
    """

    def __init__(self, data_dir: str | None = None) -> None:
        self._data_dir = data_dir if data_dir is not None else get_settings().data_dir

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

    def abs_path(self, key: str) -> Path:
        return Path(self._data_dir) / key

    def write(self, key: str, data: bytes) -> None:
        path = self.abs_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_bytes(data)
        tmp_path.rename(path)

    def exists(self, key: str) -> bool:
        return self.abs_path(key).exists()


@lru_cache(maxsize=1)
def get_storage() -> LocalStorage:
    """Return a cached LocalStorage instance using settings.data_dir."""
    return LocalStorage()
