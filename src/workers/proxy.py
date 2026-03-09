"""Proxy worker: generate image proxy and thumbnail from source asset. API-only."""

import logging
from pathlib import Path

import pyvips
import rawpy

from src.core.file_extensions import RAW_EXTENSIONS
from src.storage.local import LocalStorage
from src.workers.base import BaseWorker

logger = logging.getLogger(__name__)

PROXY_LONG_EDGE = 2048
PROXY_JPEG_QUALITY = 75
THUMBNAIL_LONG_EDGE = 256
THUMBNAIL_JPEG_QUALITY = 80


def _load_vips_image(source_path: Path) -> tuple[pyvips.Image, bool]:
    """
    Load image into pyvips. Returns (image, from_embedded_thumb).

    For RAW files:
    - Try extract_thumb() first (fast path)
    - If embedded JPEG long edge >= PROXY_LONG_EDGE: use it
    - Otherwise: fall back to full rawpy decode (slow but full quality)

    For non-RAW: load directly (no sequential — two passes needed).
    """
    ext = source_path.suffix.lower()

    if ext in RAW_EXTENSIONS:
        try:
            with rawpy.imread(str(source_path)) as raw:
                thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                img = pyvips.Image.new_from_buffer(bytes(thumb.data), "")
                long_edge = max(img.width, img.height)
                if long_edge >= PROXY_LONG_EDGE:
                    return img, True
                else:
                    logger.debug(
                        "Embedded JPEG too small (%dpx), falling back to full decode: %s",
                        long_edge,
                        source_path.name,
                    )
                    # Fall through to full decode below
            elif thumb.format == rawpy.ThumbFormat.BITMAP:
                import numpy as np

                arr = np.array(thumb.data, dtype=np.uint8)
                height, width, bands = arr.shape
                img = pyvips.Image.new_from_memory(
                    arr.tobytes(), width, height, bands, "uchar"
                )
                long_edge = max(img.width, img.height)
                if long_edge >= PROXY_LONG_EDGE:
                    return img, True
                # Fall through to full decode below
        except rawpy.LibRawNoThumbnailError:
            pass  # Fall through to full decode
        except rawpy.LibRawFileUnsupportedError:
            raise ValueError(f"Unsupported RAW format: {source_path}")

        # Full RAW decode fallback
        with rawpy.imread(str(source_path)) as raw:
            rgb = raw.postprocess()
        import numpy as np

        height, width, bands = rgb.shape
        return pyvips.Image.new_from_memory(
            rgb.tobytes(), width, height, bands, "uchar"
        ), False

    return pyvips.Image.new_from_file(str(source_path)), False


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

        img, from_thumb = _load_vips_image(source_path)
        width_orig = img.width
        height_orig = img.height

        # Generate proxy (resize down only — never upscale)
        if max(width_orig, height_orig) > PROXY_LONG_EDGE:
            proxy_img = img.thumbnail_image(
                PROXY_LONG_EDGE,
                height=PROXY_LONG_EDGE,
                size=pyvips.enums.Size.DOWN,
            )
        else:
            proxy_img = img  # already smaller than proxy size — use as-is

        proxy_bytes = proxy_img.write_to_buffer(".jpg[Q=%d]" % PROXY_JPEG_QUALITY)
        self._storage.write(proxy_key, proxy_bytes)

        # Generate thumbnail FROM PROXY — not from source
        # proxy_img is already the right size or smaller; no need to reload source
        thumb_img = proxy_img.thumbnail_image(
            THUMBNAIL_LONG_EDGE,
            height=THUMBNAIL_LONG_EDGE,
            size=pyvips.enums.Size.DOWN,
        )
        thumb_bytes = thumb_img.write_to_buffer(".jpg[Q=%d]" % THUMBNAIL_JPEG_QUALITY)
        self._storage.write(thumbnail_key, thumb_bytes)

        if from_thumb:
            logger.debug("Used embedded JPEG for %s", source_path.name)

        return {
            "proxy_key": proxy_key,
            "thumbnail_key": thumbnail_key,
            "width": width_orig,
            "height": height_orig,
        }
