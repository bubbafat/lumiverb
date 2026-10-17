"""Local cache of analysis proxies, the input to transcription and scenes.

Proxies rendered on this machine go straight in (put); others download
from the server on first use (get). Least recently used files go once the
cache is over its size, but never the one just added.

Where the server keeps them on this machine's disk (`stored`: the scheduler,
beside the API, sharing its database and DATA_DIR), they're read there, and
the cache holds no second copy: on a Mac that was up to its 50 GB again.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

from src.processing.cache_dir import cache_dir, cache_entry

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
    def __init__(self, client: object, *, cache_dir: Path | None = None, max_bytes: int | None = None,
                 stored: Callable[[str], Path | None] | None = None) -> None:
        """`stored`: the file the server keeps for a clip, when it's on this
        machine (None: not here, or none)."""
        if max_bytes is None:
            from src.processing.machine import current

            max_bytes = int(current().analysis_cache_gb * 1024**3)
        self._client = client
        self._dir = cache_dir or default_cache_dir()
        self._max_bytes = max_bytes
        self._stored = stored

    def _here(self, asset_id: str) -> Path | None:
        """The server's own file for the clip, on this machine, if it has one."""
        if self._stored is None:
            return None
        try:
            return self._stored(asset_id)
        except Exception as exc:  # noqa: BLE001 — it can't say: downloaded as before
            logger.warning("Couldn't find the stored analysis proxy for %s here: %s", asset_id, exc)
            return None

    def path_for(self, asset_id: str) -> Path:
        return cache_entry(self._dir, asset_id, ".mp4")

    def get(self, asset_id: str) -> Path | None:
        """The proxy as a local file: the server's own when it's on this
        machine (a copy cached before goes), else the cache's, downloading it
        if needed. None if the server has none."""
        path = self.path_for(asset_id)
        if (here := self._here(asset_id)) is not None:
            path.unlink(missing_ok=True)
            return here
        if path.is_file():
            os.utime(path)
            return path
        self._dir.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        try:
            with self._client.stream(f"/v1/assets/{asset_id}/artifacts/analysis_proxy") as resp:
                if resp.status_code == 404:
                    return None
                with open(part, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=_CHUNK):
                        f.write(chunk)
            part.replace(path)
        except Exception as exc:  # noqa: BLE001 — a failed download is a cache miss
            logger.warning("Downloading the analysis proxy for %s failed: %s", asset_id, exc)
            part.unlink(missing_ok=True)
            return None
        self.evict(keep=path)
        return path

    def put(self, asset_id: str, rendered: Path) -> Path:
        """Move a proxy rendered on this machine (and saved to the server)
        into the cache; when the server keeps it on this machine, it's read
        there, and the rendered copy goes."""
        if (here := self._here(asset_id)) is not None:
            rendered.unlink(missing_ok=True)
            return here
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(asset_id)
        try:
            rendered.replace(path)
        except OSError:  # another filesystem
            import shutil

            shutil.move(str(rendered), str(path))
        os.utime(path)
        self.evict(keep=path)
        return path

    def evict(self, keep: Path | None = None) -> None:
        """Remove least recently used proxies until the cache fits."""
        try:
            files = [p for p in self._dir.glob("*.mp4") if p.is_file()]
        except OSError:
            return
        stats = []
        for p in files:
            try:
                st = p.stat()
            except OSError:
                continue
            stats.append((st.st_mtime, st.st_size, p))
        total = sum(size for _, size, _ in stats)
        for _, size, p in sorted(stats):
            if total <= self._max_bytes:
                break
            if keep is not None and p == keep:
                continue
            p.unlink(missing_ok=True)
            total -= size
