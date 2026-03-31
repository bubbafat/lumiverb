"""Disk-backed proxy cache for face detection.

Stores JPEG proxy images in a temporary directory so the face detection
subprocess post-pass can read them without re-downloading from the server
or holding them all in memory.

Lifecycle:
- Created at ingest/repair start.
- Written to during image processing (ingest) or pre-generated from local
  source files (repair).
- Read by face detection subprocess batches.
- Cleaned up on graceful exit (including Ctrl+C) via atexit + signal handlers.
- Stale caches from crashed processes are pruned on next startup.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import signal
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "lumiverb-proxy-cache-"


class ProxyCache:
    """Disk-backed cache of JPEG proxy images keyed by asset_id."""

    def __init__(self) -> None:
        _prune_stale_caches()
        self._dir = Path(tempfile.mkdtemp(prefix=f"{_CACHE_PREFIX}{os.getpid()}-"))
        self._prev_sigint = None
        self._prev_sigterm = None
        self._install_cleanup()

    @property
    def path(self) -> Path:
        return self._dir

    def put(self, asset_id: str, data: bytes) -> None:
        """Write proxy bytes to cache."""
        (self._dir / asset_id).write_bytes(data)

    def get(self, asset_id: str) -> bytes | None:
        """Read proxy bytes from cache, or None if not cached."""
        p = self._dir / asset_id
        if p.exists():
            return p.read_bytes()
        return None

    def remove(self, asset_id: str) -> None:
        """Remove a single entry from the cache."""
        p = self._dir / asset_id
        p.unlink(missing_ok=True)

    def cleanup(self) -> None:
        """Delete the entire cache directory."""
        if self._dir.exists():
            shutil.rmtree(self._dir, ignore_errors=True)

    def _install_cleanup(self) -> None:
        atexit.register(self.cleanup)

        def _signal_handler(signum, frame):
            self.cleanup()
            # Re-raise with original handler
            prev = self._prev_sigint if signum == signal.SIGINT else self._prev_sigterm
            if callable(prev):
                prev(signum, frame)
            elif prev == signal.SIG_DFL:
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)

        self._prev_sigint = signal.getsignal(signal.SIGINT)
        self._prev_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)


_FACE_PROXY_LONG_EDGE = 1280  # InsightFace uses 640x640 internally; 1280 is plenty


def generate_face_proxy(source_path: Path) -> bytes:
    """Generate a JPEG proxy suitable for face detection using PIL.

    This is a lightweight alternative to ``_generate_proxy_bytes`` that
    does not require pyvips/libvips.  It handles standard image formats
    and RAW (via rawpy embedded thumbnail or full demosaic).

    Returns JPEG bytes resized to fit within 1280px on the long edge.
    """
    import io as _io

    from PIL import Image as PILImage

    ext = source_path.suffix.lower()

    # RAW files: try embedded JPEG thumbnail first, then full demosaic
    from src.core.file_extensions import RAW_EXTENSIONS

    if ext in RAW_EXTENSIONS:
        import rawpy

        with rawpy.imread(str(source_path)) as raw:
            thumb = raw.extract_thumb()
        if thumb.format == rawpy.ThumbFormat.JPEG:
            img = PILImage.open(_io.BytesIO(bytes(thumb.data)))
        else:
            with rawpy.imread(str(source_path)) as raw:
                rgb = raw.postprocess()
            img = PILImage.fromarray(rgb)
    else:
        img = PILImage.open(source_path)

    try:
        img = img.convert("RGB")
        long_edge = max(img.width, img.height)
        if long_edge > _FACE_PROXY_LONG_EDGE:
            img.thumbnail((_FACE_PROXY_LONG_EDGE, _FACE_PROXY_LONG_EDGE), PILImage.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        return buf.getvalue()
    finally:
        img.close()


def _prune_stale_caches() -> None:
    """Remove cache directories from previous runs whose process is no longer alive."""
    tmp = Path(tempfile.gettempdir())
    for entry in tmp.iterdir():
        if not entry.is_dir() or not entry.name.startswith(_CACHE_PREFIX):
            continue
        # Extract PID from directory name: lumiverb-proxy-cache-{pid}-{random}
        parts = entry.name[len(_CACHE_PREFIX) :].split("-", 1)
        try:
            pid = int(parts[0])
        except (ValueError, IndexError):
            continue
        # Check if the process is still alive
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            # Process is dead — stale cache
            logger.info("Pruning stale proxy cache: %s", entry.name)
            shutil.rmtree(entry, ignore_errors=True)
        except PermissionError:
            pass  # process exists but owned by another user
