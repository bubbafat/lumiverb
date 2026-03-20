"""Proxy worker: generate image proxy and thumbnail from source asset. API-only.

TIFF files are loaded with Pillow (not libvips) because libvips's libtiff backend
has a hardcoded 50MB cumulative allocation cap that fails on large/panorama TIFFs.
"""

import logging
from pathlib import Path

import numpy as np
import pyvips
import rawpy
from PIL import Image as PILImage

from src.core.file_extensions import RAW_EXTENSIONS
from src.storage.artifact_store import ArtifactStore
from src.workers.base import BaseWorker

logger = logging.getLogger(__name__)

PROXY_LONG_EDGE = 2048
# TIFFs use Pillow instead of libvips to avoid libtiff's 50MB cumulative allocation cap.
TIFF_EXTENSIONS = {".tif", ".tiff"}
# Heuristic guard for the Pillow fallback path.
# Pillow may decode the full pixel array before resizing, which can OOM on very large TIFFs.
# We fail after a failed pyvips attempt to keep best-effort behavior for "normal" TIFFs.
TIFF_MAX_PIXELS = 25_000_000  # ~25MP
PROXY_JPEG_QUALITY = 75
THUMBNAIL_LONG_EDGE = 256
THUMBNAIL_JPEG_QUALITY = 80


def _pil_to_vips(pil_image: "PILImage.Image") -> pyvips.Image:
    """Convert a PIL Image to a pyvips Image via numpy. Always converts to RGB."""
    pil_image = pil_image.convert("RGB")
    arr = np.asarray(pil_image, dtype=np.uint8)
    height, width, bands = arr.shape
    return pyvips.Image.new_from_memory(arr.tobytes(), width, height, bands, "uchar")


def _load_tiff_proxy_image(
    source_path: Path,
    *,
    width_orig: int,
    height_orig: int,
) -> pyvips.Image:
    """
    Best-effort TIFF proxy generation.

    Primary path: pyvips with sequential access hint.
    Fallback: Pillow + numpy -> pyvips when pyvips fails.

    Pillow fallback is guarded by a max pixel-count heuristic to avoid OOM.
    """
    pixel_count = width_orig * height_orig
    max_dim = max(width_orig, height_orig)

    # 1) Try pyvips first for best-effort behavior.
    try:
        vips_img = pyvips.Image.new_from_file(
            str(source_path),
            access=pyvips.enums.Access.SEQUENTIAL,
            fail_on=pyvips.enums.FailOn.NONE,
        )
        if max_dim > PROXY_LONG_EDGE:
            proxy_img = vips_img.thumbnail_image(
                PROXY_LONG_EDGE,
                height=PROXY_LONG_EDGE,
                size=pyvips.enums.Size.DOWN,
            )
        else:
            proxy_img = vips_img  # already smaller than proxy size — use as-is

        # Materialize the proxy so thumbnailing does not re-trigger a second source pass.
        return proxy_img.copy_memory()
    except Exception as e:
        logger.debug(
            "pyvips TIFF proxy failed; falling back to Pillow: %s (%dx%d, %d px)",
            e,
            width_orig,
            height_orig,
            pixel_count,
            exc_info=True,
        )

    # 2) Guard before Pillow decodes/resizes.
    if pixel_count > TIFF_MAX_PIXELS:
        raise RuntimeError(
            "TIFF too large to proxy safely: "
            f"{width_orig}x{height_orig} = {pixel_count} pixels "
            f"(limit={TIFF_MAX_PIXELS})."
        )

    pil_img = PILImage.open(source_path)
    try:
        if max_dim > PROXY_LONG_EDGE:
            pil_img.thumbnail((PROXY_LONG_EDGE, PROXY_LONG_EDGE), PILImage.LANCZOS)
        return _pil_to_vips(pil_img)
    finally:
        pil_img.close()


def _load_raw_image(source_path: Path) -> tuple[pyvips.Image, bool]:
    """
    Load RAW image into pyvips. Returns (image, from_embedded_thumb).

    - Try extract_thumb() first (fast path)
    - If embedded JPEG long edge >= PROXY_LONG_EDGE: use it
    - Otherwise: fall back to full rawpy decode (slow but full quality)

    Only call for files with extension in RAW_EXTENSIONS.
    """
    ext = source_path.suffix.lower()
    if ext not in RAW_EXTENSIONS:
        raise ValueError(f"Not a RAW file: {source_path}")

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
    height, width, bands = rgb.shape
    return pyvips.Image.new_from_memory(
        rgb.tobytes(), width, height, bands, "uchar"
    ), False


class ProxyWorker(BaseWorker):
    job_type = "proxy"

    def __init__(
        self,
        client: object,
        artifact_store: ArtifactStore,
        concurrency: int = 1,
        once: bool = False,
        library_id: str | None = None,
        output_mode: str = "human",
    ) -> None:
        super().__init__(
            client,
            concurrency=concurrency,
            once=once,
            library_id=library_id,
            output_mode=output_mode,
        )
        self._artifact_store = artifact_store

    def process(self, job: dict) -> dict:
        asset_id = job["asset_id"]
        rel_path = job["rel_path"]
        media_type = job["media_type"]
        root_path = job["root_path"]
        library_id = job["library_id"]

        if media_type == "video":
            logger.info("Skipping video asset_id=%s (video proxy deferred)", asset_id)
            return {}

        root = Path(root_path).resolve()
        source_path = (root / rel_path).resolve()
        if not source_path.is_relative_to(root):
            raise ValueError(f"rel_path escapes library root: {rel_path!r}")
        if not source_path.exists():
            raise FileNotFoundError(f"Source file not found: {source_path}")

        ext = source_path.suffix.lower()

        if ext in RAW_EXTENSIONS:
            img, from_thumb = _load_raw_image(source_path)
            if from_thumb:
                # Embedded JPEG was used for speed; read true sensor dimensions from RAW headers.
                with rawpy.imread(str(source_path)) as _raw:
                    _s = _raw.sizes
                    width_orig = _s.iwidth
                    height_orig = _s.iheight
            else:
                width_orig = img.width
                height_orig = img.height

            if max(width_orig, height_orig) > PROXY_LONG_EDGE:
                proxy_img = img.thumbnail_image(
                    PROXY_LONG_EDGE,
                    height=PROXY_LONG_EDGE,
                    size=pyvips.enums.Size.DOWN,
                )
            else:
                proxy_img = img  # already smaller than proxy size — use as-is

        elif ext in TIFF_EXTENSIONS:
            # TIFF: best-effort pyvips first (with sequential access hint), fall back to Pillow,
            # but guard Pillow's full-decode path against OOM on huge TIFFs.
            pil_img = PILImage.open(source_path)
            width_orig, height_orig = pil_img.size
            pil_img.close()
            proxy_img = _load_tiff_proxy_image(
                source_path,
                width_orig=width_orig,
                height_orig=height_orig,
            )
            from_thumb = False

        else:
            # All other non-RAW formats: pyvips thumbnail (exploits embedded previews)
            # Use a non-sequential header read for true source dimensions only.
            header = pyvips.Image.new_from_file(
                str(source_path),
                fail_on=pyvips.enums.FailOn.NONE,
            )
            width_orig = header.width
            height_orig = header.height
            del header

            proxy_img = pyvips.Image.thumbnail(
                str(source_path),
                PROXY_LONG_EDGE,
                height=PROXY_LONG_EDGE,
                size=pyvips.enums.Size.DOWN,
            )
            from_thumb = False

            # Materialize the proxy in memory so subsequent thumbnailing does not
            # trigger a second pass over a sequential JPEG source.
            proxy_img = proxy_img.copy_memory()

        # Generate proxy (resize down only — never upscale)
        proxy_bytes = proxy_img.write_to_buffer(".jpg[Q=%d]" % PROXY_JPEG_QUALITY)
        proxy_ref = self._artifact_store.write_artifact(
            "proxy", asset_id, proxy_bytes,
            library_id=library_id, rel_path=rel_path,
            width=width_orig, height=height_orig,
        )

        # Generate thumbnail FROM PROXY — not from source
        # proxy_img is already the right size or smaller; no need to reload source
        thumb_img = proxy_img.thumbnail_image(
            THUMBNAIL_LONG_EDGE,
            height=THUMBNAIL_LONG_EDGE,
            size=pyvips.enums.Size.DOWN,
        )
        thumb_bytes = thumb_img.write_to_buffer(".jpg[Q=%d]" % THUMBNAIL_JPEG_QUALITY)
        thumb_ref = self._artifact_store.write_artifact(
            "thumbnail", asset_id, thumb_bytes,
            library_id=library_id, rel_path=rel_path,
        )

        if from_thumb:
            logger.debug("Used embedded JPEG for %s", source_path.name)

        return {
            "proxy_key": proxy_ref.key,
            "thumbnail_key": thumb_ref.key,
            "proxy_sha256": proxy_ref.sha256,
            "thumbnail_sha256": thumb_ref.sha256,
            "width": width_orig,
            "height": height_orig,
        }
