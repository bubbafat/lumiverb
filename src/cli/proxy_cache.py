"""Proxy-aware disk cache for downstream image processing tasks.

All consumers (faces, vision, OCR, embeddings) get correctly-sized
JPEG proxies without thinking about resolution. The cache handles:

- Storing full-resolution 2048px proxies from scan (put_scan)
- Downscaling oversized images on put() for legacy callers
- Generating proxies from source files on demand via get()
- Falling back to server download when local source isn't available
- Persistent storage across runs in ~/.cache/lumiverb/proxies/
- SHA-256 sidecar files for source file change detection

Scan writes 2048px proxies + SHA sidecars. Enrich reads and resizes
as needed. The cache is the handoff point between the two phases.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_PERSISTENT_DIR = Path.home() / ".cache" / "lumiverb" / "proxies"
_DEFAULT_MAX_EDGE = 1280
_JPEG_QUALITY = 75


class ProxyCache:
    """Disk-backed cache of JPEG proxy images.

    Scan stores full-resolution 2048px proxies via put_scan() with a
    .sha sidecar for change detection. Legacy callers use put() which
    downscales to max_edge (default 1280px).

    Uses a persistent directory (~/.cache/lumiverb/proxies/) so proxies
    survive across runs. Files are keyed by asset_id.
    """

    def __init__(
        self,
        max_edge: int = _DEFAULT_MAX_EDGE,
        root_path: Path | None = None,
        client: object | None = None,
    ) -> None:
        """
        Args:
            max_edge: Maximum long edge for cached proxies (for put/get).
            root_path: Library root for local source file generation.
            client: LumiverbClient for server download fallback.
        """
        self._dir = _PERSISTENT_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_edge = max_edge
        self._root_path = root_path
        self._client = client

    @property
    def path(self) -> Path:
        return self._dir

    def put(self, asset_id: str, image_bytes: bytes) -> None:
        """Store proxy bytes, downscaling if needed. Never scales up."""
        image_bytes = self._ensure_size(image_bytes)
        self._atomic_write(self._dir / asset_id, image_bytes)

    def put_scan(self, asset_id: str, jpeg_bytes: bytes, source_sha256: str) -> None:
        """Store a full-resolution 2048px proxy from scan with SHA sidecar.

        Writes are atomic (temp + rename) so concurrent readers never see
        partial files. The .sha sidecar is written after the proxy — a
        reader that sees a .sha file can trust the proxy is complete.
        """
        self._atomic_write(self._dir / asset_id, jpeg_bytes)
        self._atomic_write(self._dir / f"{asset_id}.sha", source_sha256.encode())

    def get_sha(self, asset_id: str) -> str | None:
        """Read the SHA-256 sidecar for an asset. Returns None if missing."""
        sha_path = self._dir / f"{asset_id}.sha"
        if sha_path.exists():
            return sha_path.read_text().strip()
        return None

    def put_from_path(self, asset_id: str, source_path: Path) -> bytes:
        """Generate proxy from a source file and cache it. Returns the bytes."""
        from src.cli.proxy_gen import generate_proxy_bytes
        image_bytes, _, _ = generate_proxy_bytes(source_path, max_long_edge=self._max_edge)
        self._atomic_write(self._dir / asset_id, image_bytes)
        return image_bytes

    def has(self, asset_id: str) -> bool:
        """Check if a proxy exists in the cache without reading it."""
        return (self._dir / asset_id).exists()

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
        """Remove a single entry and its SHA sidecar from the cache."""
        (self._dir / asset_id).unlink(missing_ok=True)
        (self._dir / f"{asset_id}.sha").unlink(missing_ok=True)

    def cleanup(self) -> None:
        """Delete the entire cache directory."""
        if self._dir.exists():
            shutil.rmtree(self._dir, ignore_errors=True)

    @staticmethod
    def _atomic_write(dest: Path, data: bytes) -> None:
        """Write data to dest atomically via temp + rename."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
        try:
            os.write(fd, data)
            os.close(fd)
            fd = -1
            os.replace(tmp_path, dest)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

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
