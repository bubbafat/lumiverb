"""Server-side InsightFace provider used by hybrid similarity search.

The CLI workers use `src.client.workers.faces.insightface_provider` for
ingest-time face detection. The server uses the *same* provider class
to extract faces from query images uploaded via search-by-image — but
needs two extra setup steps that don't apply on the worker side:

1. **Writable model cache.** The systemd unit pins the API server's
   filesystem to read-only outside `${DATA_DIR}` (see deploy-api.sh —
   `ProtectSystem=strict` + `ReadWritePaths=${DATA_DIR}`). InsightFace
   downloads models to `~/.insightface/models` on first call. Without
   redirection that becomes a "Read-only file system" 500 the first
   time anyone uploads a search image. We mirror the HF cache fix in
   `src.server.embeddings.clip_provider`: set `INSIGHTFACE_HOME` to a
   writable path under data_dir before importing the package.

2. **Module-level singleton.** Workers spawn fresh per-batch
   subprocesses (the ONNX leak forces it). The API server is one
   long-lived uvicorn worker — we want a single InsightFace instance
   loaded lazily on first query and reused after that. Per-query model
   load would add ~500 ms cold-start to every search.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.client.workers.faces.insightface_provider import (
        FaceDetection,
        InsightFaceProvider,
    )

logger = logging.getLogger(__name__)

_provider: "InsightFaceProvider | None" = None
_lock = threading.Lock()


def _ensure_writable_cache() -> str:
    """Point InsightFace at a cache dir under data_dir before its first
    import. Idempotent — if `INSIGHTFACE_HOME` is already set we trust
    the operator's choice."""
    from src.server.config import get_settings

    cache_root = os.environ.get("INSIGHTFACE_HOME")
    if not cache_root:
        cache_root = os.path.join(get_settings().data_dir, "insightface-cache")
        os.environ["INSIGHTFACE_HOME"] = cache_root
    os.makedirs(cache_root, exist_ok=True)
    return cache_root


def get_provider() -> "InsightFaceProvider":
    """Lazy-load the singleton InsightFace provider, configuring the
    model cache on first use. Thread-safe with double-checked locking.
    Raises RuntimeError if the `face_recognition` extra isn't installed
    so the caller can fall back to scene-only search."""
    global _provider
    if _provider is not None:
        return _provider
    with _lock:
        if _provider is not None:
            return _provider
        cache_root = _ensure_writable_cache()
        try:
            from src.client.workers.faces.insightface_provider import (
                InsightFaceProvider,
            )
        except ImportError as exc:
            raise RuntimeError(
                "InsightFace not installed — server hybrid similarity "
                "requires `uv sync --extra face_recognition`"
            ) from exc
        provider = InsightFaceProvider()
        # Force the model load now so the first real query doesn't pay
        # the cold-start cost AND so we surface a download/permission
        # error at startup-time-of-first-call rather than mid-search.
        provider.ensure_loaded()
        _provider = provider
        logger.info("Server InsightFace provider ready (cache=%s)", cache_root)
        return _provider


def detect_faces_in_image(image_bytes: bytes) -> "list[FaceDetection]":
    """Decode bytes → PIL → run InsightFace, returning the same
    `FaceDetection` list the worker pipeline produces. Used by the
    hybrid similarity endpoint to enrich a query image with face
    embeddings before fanning out to per-face vector search."""
    import io

    from PIL import Image as PILImage

    provider = get_provider()
    img = PILImage.open(io.BytesIO(image_bytes)).convert("RGB")
    try:
        return provider.detect_faces(img)
    finally:
        img.close()
