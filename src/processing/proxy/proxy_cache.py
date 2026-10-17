"""Proxy-aware disk cache for downstream image processing tasks.

All consumers (faces, vision, OCR, embeddings) get correctly-sized
JPEG proxies without thinking about resolution. The cache handles:

- Storing full-resolution 2048px proxies from scan (put_scan)
- Downscaling oversized images on put() for legacy callers
- Downloading the server's preview when it has none (get)
- Persistent storage across runs in ~/.cache/lumiverb/proxies/
- a sidecar per proxy with its source's content hash, for change detection

It never reads an original: a producer that doesn't read the storage uses
the preview (ADR-020 decision 4).

Scan writes 2048px proxies + hash sidecars. Enrich reads and resizes
as needed. The cache is the handoff point between the two phases.

Where the API keeps the previews on this machine (the scheduler beside it),
a job reads a clip's stored preview from the Store and this cache isn't
asked (src/server/scheduler/inputs.py). The scheduler's has a cap
(LUMIVERB_PREVIEW_CACHE_GB): past it the least recently used proxies go,
each with its sidecar; the CLI's has none yet (the Mac app browses it).
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from src.processing.cache_dir import cache_dir, cache_entry
from src.shared.content_hash import SHA256

logger = logging.getLogger(__name__)

_DEFAULT_MAX_EDGE = 1280
_BARE = re.compile(r"[0-9a-f]{64}")
_JPEG_QUALITY = 75
# Writes between looks at the cap: a look reads every entry's size.
_LOOK_EVERY = 50
# A write in progress this old was left by a process that died.
_STALE_TMP_SEC = 600


def fit(image_bytes: bytes, max_edge: int) -> bytes:
    """The image at most max_edge on its long edge (a JPEG when it's made
    smaller), never larger: as it is when it fits, or can't be read."""
    try:
        from src.processing.libvips import pyvips
        img = pyvips.Image.new_from_buffer(image_bytes, "")
        if max(img.width, img.height) <= max_edge:
            return image_bytes
        proxy_img = img.thumbnail_image(
            max_edge, height=max_edge,
            size=pyvips.enums.Size.DOWN,
        )
        return proxy_img.write_to_buffer(f".jpg[Q={_JPEG_QUALITY}]")
    except Exception:
        return image_bytes  # return original if downscale fails


class ProxyCache:
    """Disk-backed cache of JPEG proxy images.

    Scan stores full-resolution 2048px proxies via put_scan() with a
    .sha sidecar (its source's content hash) for change detection. Legacy callers use put() which
    downscales to max_edge (default 1280px).

    Uses a persistent directory (~/.cache/lumiverb/proxies/) so proxies
    survive across runs. Files are keyed by asset_id.
    """

    def __init__(
        self,
        max_edge: int = _DEFAULT_MAX_EDGE,
        client: object | None = None,
        cap_bytes: int | None = None,
    ) -> None:
        """
        Args:
            max_edge: Maximum long edge for cached proxies (for put/get).
            client: ApiClient for server download fallback.
            cap_bytes: Its cap (None: this machine's preview_cache_gb; 0: none).
        """
        if cap_bytes is None:
            from src.processing.machine import current

            cap_bytes = int(current().preview_cache_gb * 1024**3)
        self._cap_bytes = cap_bytes
        self._writes = 0
        self._dir = cache_dir("proxies")
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_edge = max_edge
        self._client = client

    @property
    def path(self) -> Path:
        return self._dir

    def _entry(self, asset_id: str) -> Path:
        return cache_entry(self._dir, asset_id)

    def sha_path(self, asset_id: str) -> Path:
        """The asset's .sha sidecar: the content hash of the source the proxy
        was made from (src/shared/content_hash.py)."""
        return cache_entry(self._dir, asset_id, ".sha")

    def put(self, asset_id: str, image_bytes: bytes) -> None:
        """Store proxy bytes, downscaling if needed. Never scales up."""
        image_bytes = self._ensure_size(image_bytes)
        self._atomic_write(self._entry(asset_id), image_bytes)
        self._wrote(asset_id)

    def put_scan(self, asset_id: str, jpeg_bytes: bytes, source_hash: str) -> None:
        """Store a full-resolution 2048px proxy from scan with its hash sidecar.

        Writes are atomic (temp + rename) so concurrent readers never see
        partial files. The .sha sidecar is written after the proxy — a
        reader that sees a .sha file can trust the proxy is complete.
        """
        self._atomic_write(self._entry(asset_id), jpeg_bytes)
        self._atomic_write(self.sha_path(asset_id), source_hash.encode())
        self._wrote(asset_id)

    def get_source_hash(self, asset_id: str) -> str | None:
        """The content hash the asset's proxy was made from (its sidecar), or
        None when there's none. One written before content hashes named their
        scheme, a bare SHA-256, is read as the "sha256:" it is."""
        try:
            value = self.sha_path(asset_id).read_text().strip()
        except FileNotFoundError:
            return None
        return SHA256 + value if _BARE.fullmatch(value) else value

    def has(self, asset_id: str) -> bool:
        """Check if a proxy exists in the cache without reading it."""
        return self._entry(asset_id).exists()

    def get(self, asset_id: str, content_hash: str | None = None) -> bytes | None:
        """The asset's proxy, sized (a scan's is 2048px): the cache's, else the
        server's, downloaded and kept. One cached for other content than
        content_hash (its sidecar says) isn't the clip's: it goes, and the
        server's is downloaded. None when neither has one."""
        p = self._entry(asset_id)
        made_from = self.get_source_hash(asset_id)
        if content_hash and made_from and made_from != content_hash:
            self.remove(asset_id)
        if p.exists():
            with contextlib.suppress(OSError):
                os.utime(p)  # used now: the last to go
            return self._ensure_size(p.read_bytes())
        if self._client is not None:
            try:
                resp = self._client._client.get(self._client._url(f"/v1/assets/{asset_id}/artifacts/proxy"))
                if resp.status_code == 200:
                    image_bytes = self._ensure_size(resp.content)
                    self._atomic_write(p, image_bytes)
                    if content_hash:
                        self._atomic_write(self.sha_path(asset_id), content_hash.encode())
                    self._wrote(asset_id)
                    resp.close()
                    return image_bytes
                resp.close()
            except Exception:
                pass
        return None

    def remove(self, asset_id: str) -> None:
        """Remove a single entry and its SHA sidecar from the cache."""
        self._entry(asset_id).unlink(missing_ok=True)
        self.sha_path(asset_id).unlink(missing_ok=True)

    def _wrote(self, asset_id: str) -> None:
        """After a write: the cap looked at, on the first and then every _LOOK_EVERY-th."""
        self._writes += 1
        if self._cap_bytes and self._writes % _LOOK_EVERY == 1:
            self.evict(keep=asset_id)

    def evict(self, keep: str | None = None) -> int:
        """Past its cap, the least recently used proxies go (a read is a use),
        each with its sidecar, never `keep` (the one just written); and writes
        a dead process left. How many proxies went. Nothing without a cap."""
        if not self._cap_bytes:
            return 0
        entries: dict[str, tuple[float, int]] = {}  # asset id -> (last used, bytes with its sidecar)
        try:
            found = list(os.scandir(self._dir))
        except OSError:
            return 0
        for entry in found:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                st = entry.stat()
            except OSError:
                continue
            if entry.name.endswith(".tmp"):
                if st.st_mtime < time.time() - _STALE_TMP_SEC:
                    Path(entry.path).unlink(missing_ok=True)
                continue
            asset_id = entry.name.removesuffix(".sha")
            used, size = entries.get(asset_id, (0.0, 0))  # a sidecar alone: the first to go
            entries[asset_id] = (used if entry.name.endswith(".sha") else st.st_mtime, size + st.st_size)
        total = sum(size for _, size in entries.values())
        gone = 0
        for asset_id, (_, size) in sorted(entries.items(), key=lambda item: item[1][0]):
            if total <= self._cap_bytes:
                break
            if asset_id == keep:
                continue
            try:
                self.remove(asset_id)
            except ValueError:  # not an asset's file: left alone
                continue
            total -= size
            gone += 1
        return gone

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
        return fit(image_bytes, self._max_edge)
