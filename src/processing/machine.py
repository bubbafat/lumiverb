"""This machine's way of processing: where libraries' files are, where caches
go, and how video is rendered here. None of it changes what's made (that's
the account's, on the server: src/producers/).

Whatever runs processing says once, at its start, where these come from
(use): the scheduler from its environment (src/server/scheduler/settings.py;
/etc/lumiverb/env on the brain), the CLI from its config
(~/.lumiverb/config.json). Until then it's the defaults.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field


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
    # Where caches go instead of ~/.cache, when XDG_CACHE_HOME isn't set.
    cache_home: str = ""
    # How analysis proxies are rendered here (src/processing/video/analysis_proxy.py):
    # the encoder, where originals are decoded ("auto": the GPU through
    # Vulkan when it works; "cpu"; or an ffmpeg hwaccel such as "cuda") and
    # how many decode on the GPU at once (the rest on the CPU meanwhile).
    analysis_proxy_encoder: str = "libx264"
    analysis_proxy_decoder: str = "auto"
    gpu_decodes: int = 1
    # Analysis proxies rendered at once; 0: one per six cores, at most three.
    render_concurrency: int = 0
    analysis_cache_gb: float = 50.0
    # Face batches a detection process does before a fresh one takes over
    # (ONNX Runtime leaks: src/producers/faces/detect.py).
    face_batches_per_process: int = 20

    @property
    def renders(self) -> int:
        """Analysis proxies rendered at once here."""
        return self.render_concurrency or default_render_concurrency()

    @property
    def gpu_decodes_here(self) -> int:
        """Renders decoding on this machine's GPU at once (0 when they decode on the CPU)."""
        return self.gpu_decodes if self.analysis_proxy_decoder != "cpu" else 0

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
