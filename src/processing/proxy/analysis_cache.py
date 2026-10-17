"""Local cache of analysis proxies, the input to transcription and scenes.

Proxies rendered on this machine go in once saved (put); others download
from the server on first use (held). A FileCache (src/server/storage/cache.py)
at most LUMIVERB_ANALYSIS_CACHE_GB: past it, the least recently used go,
but never one a job holds (a transcript's ffprobe, then its ffmpeg).

Where the server keeps them on this machine's disk (the scheduler beside the
API), a job reads them from the Store and this cache isn't asked
(src/server/scheduler/inputs.py): it held up to its 50 GB again on a Mac.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

from src.processing.cache_dir import cache_dir, cache_entry
from src.server.storage.cache import FileCache
from src.server.storage.store import NotFound

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024


def default_cache_dir() -> Path:
    return cache_dir("analysis")


# What a download leaves behind when the process is killed, and what a render
# left before renders moved to the job's scratch (src/producers/runner.py).
# Eviction counts only finished proxies.
_LEFTOVERS = ("*.rendering", "*.rendering.part", "*.mp4.part")


def clear_leftovers(directory: Path | None = None) -> int:
    """Remove half-made proxies a killed process left. Returns how many.

    Only while nothing else is rendering into the cache: the worker does
    this at start, holding the worker lock.
    """
    directory = directory or default_cache_dir()
    removed = 0
    for pattern in _LEFTOVERS:
        for path in directory.glob(pattern):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


class AnalysisProxyCache:
    def __init__(self, client: object, *, cache_dir: Path | None = None, max_bytes: int | None = None) -> None:
        if max_bytes is None:
            from src.processing.machine import current

            max_bytes = int(current().analysis_cache_gb * 1024**3)
        self._client = client
        self._dir = cache_dir or default_cache_dir()
        self._files = FileCache(lambda: self._dir, cap_bytes=max_bytes)

    def path_for(self, asset_id: str) -> Path:
        return cache_entry(self._dir, asset_id, ".mp4")

    def _key(self, asset_id: str) -> str:
        return self.path_for(asset_id).name

    @contextlib.contextmanager
    def held(self, asset_id: str) -> Iterator[Path | None]:
        """The proxy as a local file for the block, never evicted meanwhile:
        the cache's, downloaded if needed. None when the server has none, or
        it couldn't be downloaded now (a cache miss)."""
        key = self._key(asset_id)
        with contextlib.ExitStack() as held:
            held.enter_context(self._files.hold(key))
            path = None
            if self._files.exists(key) or self._download(asset_id, key):
                with contextlib.suppress(NotFound):
                    path = held.enter_context(self._files.local_file(key))
            yield path

    def _download(self, asset_id: str, key: str) -> bool:
        try:
            with self._client.stream(f"/v1/assets/{asset_id}/artifacts/analysis_proxy") as resp:
                if resp.status_code == 404:
                    return False
                with self._files.writing(key, ".mp4") as partial, open(partial, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=_CHUNK):
                        f.write(chunk)
        except Exception as exc:  # noqa: BLE001 — a failed download is a cache miss
            logger.warning("Downloading the analysis proxy for %s failed: %s", asset_id, exc)
            return False
        return True

    def put(self, asset_id: str, rendered: Path) -> Path:
        """Move a proxy rendered on this machine (and saved to the server)
        into the cache: a rename on the same disk."""
        with self._files.writing(self._key(asset_id), ".mp4") as partial:
            shutil.move(str(rendered), str(partial))
            os.utime(partial)  # used now: the last to go
        return self.path_for(asset_id)
