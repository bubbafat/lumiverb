"""Local filesystem storage for proxy and thumbnail files."""

from functools import lru_cache
from pathlib import Path

from ulid import ULID

from src.core.config import get_settings


class LocalStorage:
    """
    Local filesystem storage. All paths are relative to DATA_DIR.

    Path structure:
      {data_dir}/{tenant_id}/{library_id}/proxies/{bucket}/{asset_id}_{filename}.jpg
      {data_dir}/{tenant_id}/{library_id}/thumbnails/{bucket}/{asset_id}_{filename}.jpg

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
        return f"{tenant_id}/{library_id}/proxies/{bucket:02d}/{asset_id}_{original_stem}.jpg"

    def thumbnail_key(
        self, tenant_id: str, library_id: str, asset_id: str, original_filename: str
    ) -> str:
        bucket = self._bucket_from_asset_id(asset_id)
        original_stem = Path(original_filename).stem
        return f"{tenant_id}/{library_id}/thumbnails/{bucket:02d}/{asset_id}_{original_stem}.jpg"

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
