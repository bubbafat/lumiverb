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
PROXY_JPEG_QUALITY = 85
THUMBNAIL_LONG_EDGE = 256
THUMBNAIL_JPEG_QUALITY = 80


def _load_vips_image(source_path: Path) -> pyvips.Image:
    """
    Load image into pyvips.

    For RAW files, extract the embedded JPEG via rawpy.extract_thumb() first to
    avoid full demosaicing (10–50x faster). If no embedded thumbnail exists,
    fall back to a full rawpy decode and construct a pyvips image from memory.

    For non-RAW formats, load directly with the default (random) access mode so
    the same image can be safely used for both proxy and thumbnail generation.
    """
    ext = source_path.suffix.lower()

    if ext in RAW_EXTENSIONS:
        try:
            with rawpy.imread(str(source_path)) as raw:
                thumb = raw.extract_thumb()

            if thumb.format == rawpy.ThumbFormat.JPEG:
                # Embedded JPEG — load bytes directly into pyvips
                return pyvips.Image.new_from_buffer(bytes(thumb.data), "")
            elif thumb.format == rawpy.ThumbFormat.BITMAP:
                # Embedded bitmap — convert via numpy
                import numpy as np

                arr = np.array(thumb.data, dtype=np.uint8)
                height, width, bands = arr.shape
                return pyvips.Image.new_from_memory(
                    arr.tobytes(), width, height, bands, "uchar"
                )
        except rawpy.LibRawNoThumbnailError:
            # No embedded thumb — fall back to full decode
            with rawpy.imread(str(source_path)) as raw:
                rgb = raw.postprocess()

            import numpy as np

            height, width, bands = rgb.shape
            return pyvips.Image.new_from_memory(
                rgb.tobytes(), width, height, bands, "uchar"
            )
        except rawpy.LibRawFileUnsupportedError as exc:
            raise ValueError(f"Unsupported RAW format: {source_path}") from exc

    # JPEG, PNG, TIFF, etc — use default (random) access since we make two passes
    return pyvips.Image.new_from_file(str(source_path))


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

        img = _load_vips_image(source_path)
        width_orig = img.width
        height_orig = img.height

        # Generate proxy (resize if larger than PROXY_LONG_EDGE)
        if max(width_orig, height_orig) > PROXY_LONG_EDGE:
            proxy_img = img.thumbnail_image(
                PROXY_LONG_EDGE,
                height=PROXY_LONG_EDGE,
                size=pyvips.enums.Size.DOWN,
            )
        else:
            proxy_img = img
        proxy_bytes = proxy_img.write_to_buffer(".jpg[Q=%d]" % PROXY_JPEG_QUALITY)
        self._storage.write(proxy_key, proxy_bytes)

        # Generate thumbnail — use thumbnail_image from original for quality
        thumb_img = img.thumbnail_image(
            THUMBNAIL_LONG_EDGE,
            height=THUMBNAIL_LONG_EDGE,
            size=pyvips.enums.Size.DOWN,
        )
        thumb_bytes = thumb_img.write_to_buffer(".jpg[Q=%d]" % THUMBNAIL_JPEG_QUALITY)
        self._storage.write(thumbnail_key, thumb_bytes)

        return {
            "proxy_key": proxy_key,
            "thumbnail_key": thumbnail_key,
            "width": width_orig,
            "height": height_orig,
        }
