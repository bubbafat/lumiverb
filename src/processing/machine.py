"""This machine's way of processing: where libraries' files are, where caches
go, how video is rendered here and how often libraries are scanned in full.
None of it changes what's made (that's the account's, on the server:
src/producers/).

Whatever runs processing says once, at its start, where these come from
(use): the scheduler from its environment (src/server/scheduler/settings.py;
/etc/lumiverb/env on Linux), the CLI from its config
(~/.lumiverb/config.json). Until then it's the defaults.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

# Each library is scanned in full this often, in minutes, unless set
# (LUMIVERB_FULL_SCAN_EVERY_MIN). Where nothing reports changes (a Mac running
# the server) it's how new files are found. Robert, Oct 13: his 14,608 files
# on the Mac's own disk walk in 0.4 s, and a full scan finding nothing new
# takes about 3 s.
FULL_SCAN_EVERY_MIN = 15.0


def default_render_concurrency() -> int:
    """Analysis proxies rendered at once by default: a 4K HEVC decode uses a
    few cores, so one per six, at most three (the storage's bandwidth)."""
    return max(1, min(3, (os.cpu_count() or 1) // 6))


@dataclass(frozen=True)
class Machine:
    # Library roots as the server stores them -> where they are here, by
    # path prefix (src/processing/roots.py).
    root_map: Mapping[str, str] = field(default_factory=dict)
    # Library roots read only through root_map (the scheduler: it reads every
    # account's libraries), and never one under these (the server's data).
    mapped_roots_only: bool = False
    refused_roots: tuple[str, ...] = ()
    # The server's data folder (the scheduler's): where its previews are is
    # read from it (src/shared/previews_location.py), as they may move.
    data_dir: str = ""
    # Where caches go instead of ~/.cache, when XDG_CACHE_HOME isn't set.
    cache_home: str = ""
    # How analysis proxies are rendered here (src/processing/video/analysis_proxy.py):
    # the encoder, where originals are decoded ("auto": the GPU through
    # Vulkan when it works, else a Mac's media engine through VideoToolbox,
    # src/processing/video/ffmpeg_run.py; "cpu"; or an ffmpeg hwaccel such as
    # "cuda") and how many decode on the GPU at once (the rest on the CPU
    # meanwhile; a Mac's media engine takes every render).
    analysis_proxy_encoder: str = "libx264"
    analysis_proxy_decoder: str = "auto"
    gpu_decodes: int = 1
    # Analysis proxies rendered at once; 0: one per six cores, at most three.
    render_concurrency: int = 0
    analysis_cache_gb: float = 50.0
    # The preview cache's cap (src/processing/proxy/proxy_cache.py); 0: none,
    # the CLI's (shared with the Mac app's browsing until ADR-020 phase 5).
    preview_cache_gb: float = 0.0
    # Face batches a detection process does before a fresh one takes over
    # (ONNX Runtime leaks: src/producers/faces/detect.py).
    face_batches_per_process: int = 20
    # How often the scheduler scans each library in full, in minutes. A big
    # library on a network share can take longer to walk: raise it there.
    full_scan_every_min: float = FULL_SCAN_EVERY_MIN

    def refused_here(self) -> tuple[str, ...]:
        """Folders never read as a library's, nor walked into inside one:
        refused_roots, and the server's previews folders (the server's, the
        libraries' own, and during a move, where they were)."""
        if not self.data_dir:
            return self.refused_roots
        from src.shared import previews_location

        return (*self.refused_roots, *(str(root) for root in previews_location.read(self.data_dir).roots))

    @property
    def renders(self) -> int:
        """Analysis proxies rendered at once here."""
        return self.render_concurrency or default_render_concurrency()

    @property
    def gpu_decodes_here(self) -> int:
        """Renders decoding on this machine's GPU at once: 0 when they don't,
        on the CPU ("cpu", or "auto" finding no GPU decoder) or on a Mac's
        media engine, which leaves the GPU to the models (ffmpeg_run.decoded_by,
        SHARED_MEMORY). The scheduler's GPU hold goes by it: AI machines sharing
        this GPU (a Mac's Ollama) give up no requests for renders that don't use it."""
        from src.processing.video import ffmpeg_run

        decoder = ffmpeg_run.decoded_by(self.analysis_proxy_decoder)
        return self.gpu_decodes if decoder and decoder not in ffmpeg_run.SHARED_MEMORY else 0

    def slots(self, setting: str) -> int:
        """A pool's slots, sized by one of these (a Pool's sized_by)."""
        value = getattr(self, setting)
        return int(value)


_lock = threading.Lock()
_source: Callable[[], Machine] | None = None
_current: Machine | None = None


def use(source: Machine | Callable[[], Machine] | None) -> None:
    """Where this process's machine settings come from: given, or read on
    first use (so a bad config file stops what needs it, saying why). None:
    the defaults again."""
    global _source, _current
    with _lock:
        _current = source if isinstance(source, Machine) else None
        _source = None if isinstance(source, Machine) else source


def current() -> Machine:
    global _current
    with _lock:
        if _current is None:
            _current = _source() if _source is not None else Machine()
        return _current
