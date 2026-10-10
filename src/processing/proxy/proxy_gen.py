"""Proxy image generation from source files.

Single code path for all proxy generation: ingest uploads, face detection,
and repair. Supports RAW (via rawpy + embedded thumbnail optimisation),
TIFF, and standard image formats via pyvips.

Target size is configurable — ingest uses 2048px for upload proxies,
face detection uses 1280px.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pyvips
from PIL import Image as PILImage

logger = logging.getLogger(__name__)

PROXY_LONG_EDGE = 2048
PROXY_JPEG_QUALITY = 75
FACE_PROXY_LONG_EDGE = 1280  # InsightFace uses 640x640 internally; 1280 is plenty

# TIFFs may exceed libvips/libtiff's 50MB cumulative allocation cap, forcing a
# Pillow fallback. Pillow decodes the full pixel array before resizing, so a
# guard prevents OOM on very large TIFFs. Applied only on the Pillow fallback
# path — pyvips streams sequentially and does not need the cap.
TIFF_MAX_PIXELS = 25_000_000  # ~25 MP


def _pil_to_vips(pil_image: PILImage.Image) -> pyvips.Image:
    """Convert a PIL image to a pyvips image via numpy buffer."""
    rgb = pil_image.convert("RGB")
    arr = np.asarray(rgb, dtype=np.uint8)
    height, width, bands = arr.shape
    return pyvips.Image.new_from_memory(arr.tobytes(), width, height, bands, "uchar")


def _load_tiff_proxy_image(
    source_path: Path,
    *,
    width_orig: int,
    height_orig: int,
    max_long_edge: int = PROXY_LONG_EDGE,
) -> pyvips.Image:
    """Best-effort TIFF proxy generation.

    Primary path: pyvips with sequential access hint.
    Fallback: Pillow → numpy → pyvips when pyvips fails.

    The Pillow fallback is guarded by :data:`TIFF_MAX_PIXELS` to avoid OOM on
    very large TIFFs — Pillow may decode the full pixel array before resizing.
    """
    pixel_count = width_orig * height_orig
    max_dim = max(width_orig, height_orig)

    # 1) Try pyvips first for best-effort behaviour on typical TIFFs.
    try:
        vips_img = pyvips.Image.new_from_file(
            str(source_path),
            access=pyvips.enums.Access.SEQUENTIAL,
            fail_on=pyvips.enums.FailOn.NONE,
        )
        if max_dim > max_long_edge:
            proxy_img = vips_img.thumbnail_image(
                max_long_edge,
                height=max_long_edge,
                size=pyvips.enums.Size.DOWN,
            )
        else:
            proxy_img = vips_img  # already smaller than proxy size — use as-is

        # Materialize so downstream writes don't re-trigger a second source pass.
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

    # 2) Guard before Pillow decodes/resizes the full image into memory.
    if pixel_count > TIFF_MAX_PIXELS:
        raise RuntimeError(
            "TIFF too large to proxy safely: "
            f"{width_orig}x{height_orig} = {pixel_count} pixels "
            f"(limit={TIFF_MAX_PIXELS})."
        )

    pil_img = PILImage.open(source_path)
    try:
        if max_dim > max_long_edge:
            pil_img.thumbnail((max_long_edge, max_long_edge), PILImage.LANCZOS)
        return _pil_to_vips(pil_img)
    finally:
        pil_img.close()


def generate_proxy_bytes(source_path: Path, max_long_edge: int = PROXY_LONG_EDGE) -> tuple[bytes, int, int]:
    """Generate a resized JPEG proxy from a source image.

    Returns (jpeg_bytes, width_orig, height_orig).

    Handles RAW files (via rawpy — prefers embedded JPEG thumbnail when
    large enough), TIFF, and standard image formats.  Uses pyvips for
    speed; falls back to PIL for TIFF when vips fails.
    """
    from src.shared.file_extensions import RAW_EXTENSIONS

    ext = source_path.suffix.lower()
    TIFF_EXTENSIONS = {".tif", ".tiff"}

    if ext in RAW_EXTENSIONS:
        import rawpy

        try:
            with rawpy.imread(str(source_path)) as raw:
                thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                img = pyvips.Image.new_from_buffer(bytes(thumb.data), "")
                long_edge = max(img.width, img.height)
                if long_edge >= max_long_edge:
                    with rawpy.imread(str(source_path)) as _raw:
                        _s = _raw.sizes
                        width_orig = _s.iwidth
                        height_orig = _s.iheight
                    proxy_img = img.thumbnail_image(
                        max_long_edge, height=max_long_edge,
                        size=pyvips.enums.Size.DOWN,
                    )
                    proxy_bytes = proxy_img.write_to_buffer(".jpg[Q=%d]" % PROXY_JPEG_QUALITY)
                    logger.info("proxy: %s — embedded JPEG %dx%d → %dpx (%d bytes)",
                                source_path.name, img.width, img.height, max_long_edge, len(proxy_bytes))
                    return proxy_bytes, width_orig, height_orig
                else:
                    logger.info("proxy: %s — embedded JPEG too small (%dx%d < %dpx), demosaicing",
                                source_path.name, img.width, img.height, max_long_edge)
        except Exception:
            pass

        try:
            logger.info("proxy: %s — RAW demosaic", source_path.name)
            with rawpy.imread(str(source_path)) as raw:
                rgb = raw.postprocess()
                _s = raw.sizes
                width_orig = _s.iwidth
                height_orig = _s.iheight
            h, w, bands = rgb.shape
            img = pyvips.Image.new_from_memory(rgb.tobytes(), w, h, bands, "uchar")
            proxy_img = img.thumbnail_image(
                max_long_edge, height=max_long_edge,
                size=pyvips.enums.Size.DOWN,
            )
            proxy_bytes = proxy_img.write_to_buffer(".jpg[Q=%d]" % PROXY_JPEG_QUALITY)
            return proxy_bytes, width_orig, height_orig
        except Exception:
            # rawpy can't handle this file (e.g. Adobe-generated DNG) —
            # try pyvips directly as it handles many DNG variants
            logger.info("proxy: %s — rawpy failed, trying pyvips direct", source_path.name)
            header = pyvips.Image.new_from_file(
                str(source_path), fail_on=pyvips.enums.FailOn.NONE,
            )
            width_orig = header.width
            height_orig = header.height
            del header
            proxy_img = pyvips.Image.thumbnail(
                str(source_path), max_long_edge,
                height=max_long_edge,
                size=pyvips.enums.Size.DOWN,
            ).copy_memory()
            proxy_bytes = proxy_img.write_to_buffer(".jpg[Q=%d]" % PROXY_JPEG_QUALITY)
            return proxy_bytes, width_orig, height_orig

    elif ext in TIFF_EXTENSIONS:
        pil_img = PILImage.open(source_path)
        width_orig, height_orig = pil_img.size
        pil_img.close()
        proxy_img = _load_tiff_proxy_image(
            source_path,
            width_orig=width_orig,
            height_orig=height_orig,
            max_long_edge=max_long_edge,
        )
        proxy_bytes = proxy_img.write_to_buffer(".jpg[Q=%d]" % PROXY_JPEG_QUALITY)
        return proxy_bytes, width_orig, height_orig

    else:
        header = pyvips.Image.new_from_file(
            str(source_path), fail_on=pyvips.enums.FailOn.NONE,
        )
        width_orig = header.width
        height_orig = header.height
        del header

        proxy_img = pyvips.Image.thumbnail(
            str(source_path), max_long_edge,
            height=max_long_edge,
            size=pyvips.enums.Size.DOWN,
        ).copy_memory()
        proxy_bytes = proxy_img.write_to_buffer(".jpg[Q=%d]" % PROXY_JPEG_QUALITY)
        return proxy_bytes, width_orig, height_orig


def generate_face_proxy(source_path: Path) -> bytes:
    """Generate a JPEG proxy suitable for face detection.

    Same pipeline as upload proxies but at 1280px instead of 2048px.
    """
    jpeg_bytes, _, _ = generate_proxy_bytes(source_path, max_long_edge=FACE_PROXY_LONG_EDGE)
    return jpeg_bytes
