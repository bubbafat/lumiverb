"""Proxy worker: generate image proxy and thumbnail from source asset."""

import logging
from pathlib import Path

import pyvips
from sqlmodel import Session

from src.models.tenant import WorkerJob
from src.repository.tenant import AssetRepository, LibraryRepository
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
        tenant_session: Session,
        tenant_id: str,
        concurrency: int = 1,
        once: bool = False,
        library_id: str | None = None,
    ) -> None:
        super().__init__(
            tenant_session,
            concurrency=concurrency,
            once=once,
            library_id=library_id,
        )
        self._tenant_id = tenant_id
        self._asset_repo = AssetRepository(tenant_session)
        self._library_repo = LibraryRepository(tenant_session)
        self._storage = LocalStorage()

    def process(self, job: WorkerJob) -> None:
        if job.asset_id is None:
            raise ValueError("Job has no asset_id")
        asset = self._asset_repo.get_by_id(job.asset_id)
        if asset is None:
            raise ValueError(f"Asset not found: {job.asset_id}")

        if asset.media_type == "video":
            logger.info("Skipping video asset_id=%s (video proxy deferred)", asset.asset_id)
            return

        library = self._library_repo.get_by_id(asset.library_id)
        if library is None:
            raise ValueError(f"Library not found: {asset.library_id}")
        source_path = Path(library.root_path) / asset.rel_path
        if not source_path.exists():
            raise FileNotFoundError(f"Source file not found: {source_path}")

        proxy_key = self._storage.proxy_key(
            self._tenant_id, asset.library_id, asset.asset_id, asset.rel_path
        )
        thumbnail_key = self._storage.thumbnail_key(
            self._tenant_id, asset.library_id, asset.asset_id, asset.rel_path
        )
        if self._storage.exists(proxy_key) and self._storage.exists(thumbnail_key):
            return

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

        self._asset_repo.update_proxy(
            asset.asset_id,
            proxy_key=proxy_key,
            thumbnail_key=thumbnail_key,
            width=width_orig,
            height=height_orig,
        )
