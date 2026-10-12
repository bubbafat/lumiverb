"""The images the API server reads: JPEG, PNG and WebP, up to MAX_PIXELS.

Wherever the server decodes a picture it was sent or has stored (a preview
or thumbnail at ingest and upload, the picture a search is by, a proxy cut
into face crops or embedded, a video face's crop), it opens it here: only
those formats' readers look at the bytes, and the size in the header is
checked before any pixel is decoded."""

from __future__ import annotations

import io
from collections.abc import Sequence

from PIL import Image

FORMATS = ("JPEG", "PNG", "WEBP")  # what clients send previews and searches as
# 120 megapixels: a 100 MP camera's photo (11,656 x 8,742) or a 108 MP phone's,
# with room to spare. What clients send is smaller (a preview is at most 2048
# px, a scene's frame a video's size, the picture a search is by 768 to 2048
# px), so this bounds what one decode takes (360 MB as RGB), not what's sent.
MAX_PIXELS = 120_000_000

_NAMES = {"JPEG": "JPEG", "PNG": "PNG", "WEBP": "WebP"}


class NotAnImageError(ValueError):
    """Not an image the server reads: not one of FORMATS, more than
    MAX_PIXELS, or its pixels don't decode. The message says which."""


def _named(formats: Sequence[str]) -> str:
    names = [_NAMES[f] for f in formats]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"


def opened(data: bytes, formats: Sequence[str] = FORMATS) -> Image.Image:
    """`data` opened, its header read and no pixel decoded yet; NotAnImageError
    unless it's one of `formats` (some of FORMATS) of at most MAX_PIXELS. The
    caller closes it."""
    try:
        img = Image.open(io.BytesIO(data), formats=list(formats))
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        # Over Pillow's own limit (its warning, when warnings are errors).
        raise NotAnImageError("has more pixels than the server reads") from None
    except OSError:
        raise NotAnImageError(f"isn't a {_named(formats)} image") from None
    w, h = img.size
    if w * h > MAX_PIXELS:
        img.close()
        raise NotAnImageError(
            f"is {w} x {h} pixels, more than the {MAX_PIXELS // 1_000_000} megapixels the server reads")
    return img


def decoded(data: bytes) -> Image.Image:
    """`data` opened (as opened) and its pixels decoded; NotAnImageError also
    when they don't decode. The caller closes it."""
    img = opened(data)
    try:
        img.load()
    except (OSError, SyntaxError, EOFError) as exc:
        img.close()
        raise NotAnImageError(f"doesn't decode ({exc})") from None
    return img


def rgb(data: bytes) -> Image.Image:
    """`data` decoded (as decoded), as RGB."""
    with decoded(data) as img:
        return img.convert("RGB")
