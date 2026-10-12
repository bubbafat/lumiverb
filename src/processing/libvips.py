"""libvips as Lumiverb opens images with it: a file by its exact name, and
with libvips's trusted loaders only.

Import pyvips from here, never directly: importing this module is what
blocks the untrusted loaders in a process (the scheduler and its producers,
a scan), the ones libvips itself marks untrusted (ImageMagick, PDF, SVG,
libraw, JPEG XL, PPM and others). libvips picks a loader by a file's
content, not its name. A library's images are read with trusted ones:
JPEG, PNG, WebP and TIFF (a DNG rawpy can't read is a TIFF too). RAW is
read by rawpy, and BMP by Pillow (proxy_gen), as libvips reads BMP only
with ImageMagick.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pyvips

logger = logging.getLogger(__name__)

TRUSTED_ONLY_SINCE = (8, 13)  # the first libvips that can block its untrusted loaders


def _block_untrusted() -> None:
    version = (pyvips.version(0), pyvips.version(1))
    if version < TRUSTED_ONLY_SINCE or not hasattr(pyvips, "block_untrusted_set"):
        logger.warning(
            "libvips %d.%d can't block its untrusted loaders (8.13 or later can): they stay on", *version,
        )
        return
    pyvips.block_untrusted_set(True)


_block_untrusted()


def open_file(path: Path | str, **options) -> pyvips.Image:
    """The image in the file at `path`, opened by that exact name, with the
    loader's `options`. libvips reads a name ending in [...] as load options
    (photo.jpg[shrink=8] is photo.jpg, shrunk), so the file is opened as a
    source, which takes the name as it is."""
    return pyvips.Image.new_from_source(pyvips.Source.new_from_file(os.fsencode(path)), "", **options)


def thumbnail_file(path: Path | str, width: int, **options) -> pyvips.Image:
    """pyvips.Image.thumbnail of the file at `path`, opened by that exact name
    (open_file)."""
    return pyvips.Image.thumbnail_source(pyvips.Source.new_from_file(os.fsencode(path)), width, **options)
