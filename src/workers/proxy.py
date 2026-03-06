"""Proxy worker: generate image proxy and thumbnail from source asset. API-only."""

import logging
from pathlib import Path

import pyvips

from src.storage.local import LocalStorage
from src.workers.base import BaseWorker

logger = logging.getLogger(__name__)

PROXY_LONG_EDGE = 2048
PROXY_JPEG_QUALITY = 85
THUMBNAIL_LONG_EDGE = 256
THUMBNAIL_JPEG_QUALITY = 80


class ProxyWorker(BaseWorker):
    job_type = "proxy"

    def __init__(
        self,
        client: object,
        storage: LocalStorage,
        tenant_id: str,
        concurrency: int = 1,
        once: bool = False,
        library_id: str | None = None,
    ) -> None:
        super().__init__(client, concurrency=concurrency, once=once, library_id=library_id)
        self._storage = storage
        self._tenant_id = tenant_id

    def process(self, job: dict) -> dict:
        asset_id = job["asset_id"]
        rel_path = job["rel_path"]
        media_type = job["media_type"]
        root_path = job["root_path"]
        library_id = job["library_id"]

        if media_type == "video":
            logger.info("Skipping video asset_id=%s (video proxy deferred)", asset_id)
            return {}

        source_path = Path(root_path) / rel_path
        if not source_path.exists():
            raise FileNotFoundError(f"Source file not found: {source_path}")

        proxy_key = self._storage.proxy_key(
            self._tenant_id, library_id, asset_id, rel_path
        )
        thumbnail_key = self._storage.thumbnail_key(
            self._tenant_id, library_id, asset_id, rel_path
        )

        img = pyvips.Image.new_from_file(str(source_path))
        width_orig = img.width
        height_orig = img.height

        if max(width_orig, height_orig) > PROXY_LONG_EDGE:
            proxy_img = img.thumbnail_image(PROXY_LONG_EDGE)
        else:
            proxy_img = img
        proxy_bytes = proxy_img.write_to_buffer(".jpg", Q=PROXY_JPEG_QUALITY)
        self._storage.write(proxy_key, proxy_bytes)

        thumb_img = img.thumbnail_image(THUMBNAIL_LONG_EDGE)
        thumb_bytes = thumb_img.write_to_buffer(".jpg", Q=THUMBNAIL_JPEG_QUALITY)
        self._storage.write(thumbnail_key, thumb_bytes)

        return {
            "proxy_key": proxy_key,
            "thumbnail_key": thumbnail_key,
            "width": width_orig,
            "height": height_orig,
        }
