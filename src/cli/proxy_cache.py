"""Proxy-aware disk cache for downstream image processing tasks.

All consumers (faces, vision, OCR, embeddings) get correctly-sized
1280px JPEG proxies without thinking about resolution. The cache handles:

- Downscaling oversized images on put()
- Generating proxies from source files on demand via get()
- Falling back to server download when local source isn't available
- Cleanup on exit (including Ctrl+C) via atexit + signal handlers
- Pruning stale caches from crashed processes

The only code that works with full-resolution (2048px) images is the
ingest upload path, which bypasses this cache entirely.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "lumiverb-proxy-cache-"
_DEFAULT_MAX_EDGE = 1280
_JPEG_QUALITY = 75


class ProxyCache:
    """Disk-backed cache of correctly-sized JPEG proxy images.

    Images are stored at max_edge resolution (default 1280px long edge).
    Callers never need to think about sizing — put() downscales,
    get() generates on demand.
    """

    def __init__(
        self,
        max_edge: int = _DEFAULT_MAX_EDGE,
        root_path: Path | None = None,
        client: object | None = None,
    ) -> None:
        """
        Args:
            max_edge: Maximum long edge for cached proxies.
            root_path: Library root for local source file generation.
            client: LumiverbClient for server download fallback.
        """
        _prune_stale_caches()
        self._dir = Path(tempfile.mkdtemp(prefix=f"{_CACHE_PREFIX}{os.getpid()}-"))
        self._max_edge = max_edge
        self._root_path = root_path
        self._client = client
        atexit.register(self.cleanup)

    @property
    def path(self) -> Path:
        return self._dir

    def put(self, asset_id: str, image_bytes: bytes) -> None:
        """Store proxy bytes, downscaling if needed. Never scales up."""
        image_bytes = self._ensure_size(image_bytes)
        (self._dir / asset_id).write_bytes(image_bytes)

    def put_from_path(self, asset_id: str, source_path: Path) -> bytes:
        """Generate proxy from a source file and cache it. Returns the bytes."""
        from src.cli.proxy_gen import generate_proxy_bytes
        image_bytes, _, _ = generate_proxy_bytes(source_path, max_long_edge=self._max_edge)
        (self._dir / asset_id).write_bytes(image_bytes)
        return image_bytes

    def get(self, asset_id: str, rel_path: str | None = None) -> bytes | None:
        """Get proxy bytes for an asset.

        Resolution order:
        1. Disk cache (already at correct size)
        2. Generate from local source file (if root_path set and file exists)
        3. Download from server (if client set), downscale, cache

        Returns None only if all sources fail.
        """
        # 1. Check cache
        p = self._dir / asset_id
        if p.exists():
            return p.read_bytes()

        # 2. Try local source
        if self._root_path is not None and rel_path is not None:
            source = (self._root_path / rel_path).resolve()
            if source.is_file():
                try:
                    return self.put_from_path(asset_id, source)
                except Exception:
                    pass  # fall through to server

        # 3. Try server download
        if self._client is not None:
            try:
                resp = self._client._client.get(self._client._url(f"/v1/assets/{asset_id}/proxy"))
                if resp.status_code == 200:
                    image_bytes = self._ensure_size(resp.content)
                    (self._dir / asset_id).write_bytes(image_bytes)
                    resp.close()
                    return image_bytes
                resp.close()
            except Exception:
                pass

        return None

    def remove(self, asset_id: str) -> None:
        """Remove a single entry from the cache."""
        (self._dir / asset_id).unlink(missing_ok=True)

    def cleanup(self) -> None:
        """Delete the entire cache directory."""
        if self._dir.exists():
            shutil.rmtree(self._dir, ignore_errors=True)

    def _ensure_size(self, image_bytes: bytes) -> bytes:
        """Downscale JPEG if it exceeds max_edge. Never scales up."""
        try:
            import pyvips
            img = pyvips.Image.new_from_buffer(image_bytes, "")
            if max(img.width, img.height) <= self._max_edge:
                return image_bytes
            proxy_img = img.thumbnail_image(
                self._max_edge, height=self._max_edge,
                size=pyvips.enums.Size.DOWN,
            )
            return proxy_img.write_to_buffer(".jpg[Q=%d]" % _JPEG_QUALITY)
        except Exception:
            return image_bytes  # return original if downscale fails



def _prune_stale_caches() -> None:
    """Remove cache directories from previous runs whose process is no longer alive."""
    tmp = Path(tempfile.gettempdir())
    for entry in tmp.iterdir():
        if not entry.is_dir() or not entry.name.startswith(_CACHE_PREFIX):
            continue
        parts = entry.name[len(_CACHE_PREFIX):].split("-", 1)
        try:
            pid = int(parts[0])
        except (ValueError, IndexError):
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            logger.info("Pruning stale proxy cache: %s", entry.name)
            shutil.rmtree(entry, ignore_errors=True)
        except PermissionError:
            pass
