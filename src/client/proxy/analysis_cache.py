"""Local cache of analysis proxies, the input to transcription and scenes.

Proxies rendered on this machine go straight in (put); others download
from the server on first use (get). Least recently used files go once the
cache is over its size, but never the one just added.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024


def default_cache_dir() -> Path:
    return Path.home() / ".cache" / "lumiverb" / "analysis"


class AnalysisProxyCache:
    def __init__(self, client: object, *, cache_dir: Path | None = None, max_bytes: int | None = None) -> None:
        if max_bytes is None:
            from src.client.cli.config import load_config

            max_bytes = int(load_config().analysis_cache_gb * 1024**3)
        self._client = client
        self._dir = cache_dir or default_cache_dir()
        self._max_bytes = max_bytes

    def path_for(self, asset_id: str) -> Path:
        return self._dir / f"{asset_id}.mp4"

    def get(self, asset_id: str) -> Path | None:
        """The proxy as a local file, downloading it if needed. None if the server has none."""
        path = self.path_for(asset_id)
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
        """Move a proxy rendered on this machine into the cache."""
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
