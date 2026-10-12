"""A preview image as the server stores it (proxies, thumbnails, scene
frames): re-encoded, its rotation applied to the pixels and its colour
profile kept, and nothing else of the original's metadata (EXIF: where,
when, which camera; XMP; comments). Previews are what a public page's
visitors see, so they carry the picture and nothing more."""

from __future__ import annotations

import io

from PIL import Image, ImageOps

FORMATS = ["JPEG", "PNG", "WEBP"]  # what clients send previews as


class NotAnImageError(ValueError):
    """Not a JPEG, PNG or WebP image."""


def reencoded(data: bytes, *, fmt: str, quality: int, max_long_edge: int | None = None) -> tuple[bytes, int, int]:
    """(bytes, width, height) of `data` as `fmt`, at most `max_long_edge` on
    its long side."""
    try:
        img = Image.open(io.BytesIO(data), formats=FORMATS)
        img.load()
    except (OSError, Image.DecompressionBombError) as exc:
        raise NotAnImageError(str(exc)) from None
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
