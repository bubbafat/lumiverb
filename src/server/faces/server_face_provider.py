"""The faces in a picture searched by (search by image's hybrid similarity),
found in a process of its own.

The picture is checked here first (src/server/images.py: JPEG, PNG or WebP,
at most 120 MP), before any model. Then InsightFace looks at it in a child
process, the faces producer's (src/producers/faces/detect.py FaceDetector):
ONNX Runtime leaks ~35 MB a picture with no fix, so the API's own process
never loads the model, the child is replaced after PER_PROCESS pictures,
and it's let go of once nothing has asked for IDLE_SEC (its memory back to
the machine). A picture it hasn't answered for in TIMEOUT_SEC is given up
and the child replaced; the search goes on by the picture's look alone.

The model is kept in the models folder (DATA_DIR/models,
src/processing/models_dir.py: model_root), the scheduler's too, so search
by image and the faces producer share one copy. The API's unit may write
only under DATA_DIR (deploy-api.sh: ProtectSystem=strict).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.processing.workers.faces.insightface_provider import FaceDetection
    from src.producers.faces.detect import FaceDetector

logger = logging.getLogger(__name__)

# Pictures a child looks at before it's replaced (its leak, ~35 MB each, given back).
PER_PROCESS = 20
# A picture not answered for in this long is given up, and the child replaced.
TIMEOUT_SEC = 60.0
# Nothing asked for this long: the child is let go of (loading the model again costs seconds).
IDLE_SEC = 300.0
# How often that's looked at.
LOOK_SEC = 30.0

_detector: FaceDetector | None = None
_last_used = 0.0
_lock = threading.Lock()


def _ensure_writable_cache() -> str:
    """Where InsightFace's model is kept (model_root: the models folder, or
    an operator's INSIGHTFACE_HOME), made if it isn't there yet."""
    from src.processing.workers.faces.insightface_provider import model_root

    cache_root = model_root()
    os.makedirs(cache_root, exist_ok=True)
    return str(cache_root)


def _the_detector() -> FaceDetector:
    """The child process's keeper, made at the first search (and the thread
    that lets the child go once idle)."""
    global _detector
    with _lock:
        if _detector is None:
            from src.producers.faces.detect import FaceDetector

            _ensure_writable_cache()
            _detector = FaceDetector(PER_PROCESS, timeout_sec=TIMEOUT_SEC)
            threading.Thread(target=_keep_letting_go, name="face-search-idle", daemon=True).start()
        return _detector


def let_go_if_idle(now: float | None = None) -> bool:
    """Let the child go when nothing has asked for IDLE_SEC and no search is
    under way. True when it was let go of."""
    detector = _detector
    if detector is None or (now if now is not None else time.monotonic()) - _last_used < IDLE_SEC:
        return False
    return detector.close_if_idle()


def _keep_letting_go() -> None:
    while True:
        time.sleep(LOOK_SEC)
        try:
            if let_go_if_idle():
                logger.info("Face search's process let go of: idle")
        except Exception:  # noqa: BLE001 — looked at again next time
            logger.exception("face search: couldn't let its idle process go")


def detect_faces_in_image(image_bytes: bytes) -> list[FaceDetection]:
    """The faces InsightFace finds in the picture, as the faces producer
    finds them (its default settings), each with its embedding. NotAnImageError
    (before any model) unless it's an image the server reads. RuntimeError
    when the child couldn't look at it, and the producer's Died when it died
    or hung: the search falls back to the picture's look."""
    global _last_used
    from src.processing.workers.faces.insightface_provider import FaceDetection
    from src.server.images import rgb

    rgb(image_bytes).close()  # refused here, never handed to the child
    detector = _the_detector()
    try:
        found = detector.detect(image_bytes, {})
    finally:
        _last_used = time.monotonic()
    if "error" in found:
        raise RuntimeError(f"face search couldn't look at the picture: {found['error']}")
    return [FaceDetection(bounding_box=f["bounding_box"], detection_confidence=f["detection_confidence"],
                          embedding=f["embedding"]) for f in found["faces"]]


def close() -> None:
    """Let the child go (the API stopping)."""
    with _lock:
        detector = _detector
    if detector is not None:
        detector.close()
