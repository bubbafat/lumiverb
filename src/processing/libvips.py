"""libvips as Lumiverb opens images with it: a file by its exact name, and
with libvips's trusted loaders only.

Import pyvips from here, never directly: importing this module is what
blocks the untrusted loaders in a process (the scheduler and its producers,
a scan), the ones libvips itself marks untrusted (ImageMagick, PDF, SVG,
libraw, JPEG XL, PPM and others). libvips picks a loader by a file's
content, not its name. A library's images are read with trusted ones:
JPEG, PNG, WebP, TIFF (a DNG rawpy can't read is a TIFF too) and HEIF
(an iPhone's HEIC photos, through libheif). RAW is
read by rawpy, and BMP by Pillow (proxy_gen), as libvips reads BMP only
with ImageMagick.

A libvips older than 8.13 can't block them, so it isn't used: importing
this module raises TooOld, saying the version found and the one needed,
every time it's tried (a module whose import failed isn't kept). Nothing
imports it as a process starts (the API never does): it comes in with the
first scan (src/processing/scan.py, through ingest and proxy_gen), which
fails with it, and the scheduler says so as that library's scan failing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pyvips

TRUSTED_ONLY_SINCE = (8, 13)  # the first libvips that can block its untrusted loaders: an older one isn't used


class TooOld(ImportError):
    """This machine's libvips can't block its untrusted loaders: Lumiverb doesn't use it."""


def _block_untrusted() -> None:
    found = (pyvips.version(0), pyvips.version(1))
    if found < TRUSTED_ONLY_SINCE:
        needed = ".".join(map(str, TRUSTED_ONLY_SINCE))
        raise TooOld(
            f"libvips {found[0]}.{found[1]} is too old: Lumiverb reads photos with libvips {needed} or later, the "
            "first that can turn off the loaders libvips doesn't trust (ImageMagick, PDF, SVG and others). Install "
            "a newer libvips (Ubuntu 24.04 or later has one), then restart Lumiverb."
        )
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
