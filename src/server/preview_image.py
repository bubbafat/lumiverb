"""A preview image as the server stores it (proxies, thumbnails, scene
frames): re-encoded, its rotation applied to the pixels and its colour
profile kept, and nothing else of the original's metadata (EXIF: where,
when, which camera; XMP; comments). Previews are what a public page's
visitors see, so they carry the picture and nothing more."""

from __future__ import annotations

import io

from PIL import Image, ImageOps

from src.server.images import NotAnImageError, decoded  # noqa: F401 — what these raise

THUMBNAIL_LONG_EDGE = 512
THUMBNAIL_WEBP_QUALITY = 80


def thumbnail(proxy: bytes) -> bytes:
    """A clip's thumbnail, made from its stored preview (already upright and
    clean): WebP, at most THUMBNAIL_LONG_EDGE on its long side. At ingest, and
    again when the thumbnail's file is lost (src/server/missing_artifacts.py)."""
    img = decoded(proxy)  # NotAnImageError unless it's an image the server reads (images.py)
    try:
        rgb = img.convert("RGB")
        w, h = rgb.size
        if max(w, h) > THUMBNAIL_LONG_EDGE:
            scale = THUMBNAIL_LONG_EDGE / max(w, h)
            rgb = rgb.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        rgb.save(buf, format="WEBP", quality=THUMBNAIL_WEBP_QUALITY)
        return buf.getvalue()
    finally:
        img.close()


def reencoded(data: bytes, *, fmt: str, quality: int, max_long_edge: int | None = None) -> tuple[bytes, int, int]:
    """(bytes, width, height) of `data` as `fmt`, at most `max_long_edge` on
    its long side."""
    img = decoded(data)
    try:
        icc = img.info.get("icc_profile")
        upright = ImageOps.exif_transpose(img) or img  # the rotation into the pixels; the tag goes with the rest
        rgb = upright.convert("RGB")
        w, h = rgb.size
        if max_long_edge and max(w, h) > max_long_edge:
            scale = max_long_edge / max(w, h)
            w, h = int(w * scale), int(h * scale)
            rgb = rgb.resize((w, h), Image.LANCZOS)
        buf = io.BytesIO()
        rgb.save(buf, format=fmt, quality=quality, **({"icc_profile": icc} if icc else {}))
        return buf.getvalue(), w, h
    finally:
        img.close()
