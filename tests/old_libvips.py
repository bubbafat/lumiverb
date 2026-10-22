"""A libvips too old to use, for a test: this process's pyvips says it's
`version`, and the modules that bring libvips in (src/processing/libvips.py,
and the scan through ingest and proxy_gen) are imported afresh, as on a
machine that has one. monkeypatch puts it all back."""

from __future__ import annotations

import sys

import pytest

# Each imports the next as it's imported: the scan, ingest, proxy_gen, libvips.
BRINGS_LIBVIPS = ("src.processing.scan", "src.processing.ingest", "src.processing.proxy.proxy_gen",
                  "src.processing.libvips")


def pretend(monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int] = (8, 12, 1)) -> None:
    import src.processing.libvips  # noqa: F401 — imported (as this machine's is new enough) to be put back after
    from src.processing.libvips import pyvips

    monkeypatch.setattr(pyvips, "version", lambda i: version[i])
    for name in BRINGS_LIBVIPS:
        monkeypatch.delitem(sys.modules, name, raising=False)
