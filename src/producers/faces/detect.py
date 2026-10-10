"""Face detection in a process of its own, one photo a call (InsightFace).

ONNX Runtime leaks ~35 MB per inference with no fix, so detection runs in a
child process, replaced after so many photos: the OS takes the memory back.
The process is kept between jobs (the model loads once per so many photos)
and shared by every account: it only looks at images, and the photos'
faces are saved by the parent, through the account's API client
(src/producers/faces/work.py), so an error saving is judged like any
producer's (src/producers/runner.py).

A process that dies mid-photo (out of memory, a segfault) never answers:
after DETECT_TIMEOUT_SEC the photo is given up (Died, a crash: counted,
uncharged) and the process replaced. Let go of meanwhile (a stop, the
model let go of while idle), the call ends at once (Stopped).
"""

from __future__ import annotations

import io
import logging
import multiprocessing as mp
import threading
import time
from collections.abc import Callable
from typing import Any

from src.producers.runner import Died, Stopped

logger = logging.getLogger(__name__)

# One photo that takes longer than this is given up (the process is replaced).
DETECT_TIMEOUT_SEC = 300.0
# How often a call looks up from its wait (its process let go of).
POLL_SEC = 1.0


def silence_stdout() -> None:
    """The child's stdout to /dev/null: InsightFace and ONNX print a lot."""
    import os

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.close(devnull)


# The child's face model, kept between photos (loading it costs seconds);
# a new one only when the detector's size changes.
_PROVIDER: Any = None


def _provider(settings: dict) -> Any:
    from src.processing.workers.faces.insightface_provider import FaceSettings, InsightFaceProvider

    global _PROVIDER
    wanted = FaceSettings.for_producer(settings)
    if _PROVIDER is None or _PROVIDER.settings.det_size != wanted.det_size:
        _PROVIDER = InsightFaceProvider(wanted)
    else:
        _PROVIDER.settings = wanted
    _PROVIDER.ensure_loaded()
    return _PROVIDER


def detect_in_child(image: bytes, settings: dict) -> dict:
    """In the child: the faces found in an image, as the server takes them, and
    the models that found them; {"error"} when the image couldn't be looked at
    (the photo's trouble, or the GPU's: the parent says whose). The model not
    loading raises: it's this machine's trouble (a crash in the parent), never
    a photo's."""
    import warnings

    from PIL import Image as PILImage

    warnings.filterwarnings("ignore", category=FutureWarning, module="insightface")
    try:
        img = PILImage.open(io.BytesIO(image)).convert("RGB")  # a photo that can't be read: no model needed
    except Exception as e:  # noqa: BLE001 — the photo's
        return {"error": str(e) or type(e).__name__}
    try:
        provider = _provider(settings)  # raises: the parent's Died, uncharged
        try:
            found = provider.detect_faces(img)
        except Exception as e:  # noqa: BLE001 — sent back, whose it is decided there (the GPU's memory, say)
            return {"error": str(e) or type(e).__name__}
    finally:
        img.close()
    return {
        "detection_model": provider.model_id,
        "detection_model_version": provider.model_version,
        # InsightFace's model pack embeds the faces it finds.
        "embedding_model": provider.model_version,
        "faces": [{"bounding_box": d.bounding_box, "detection_confidence": d.detection_confidence,
                   "embedding": d.embedding} for d in found],
    }


class FaceDetector:
    """The child process, kept between calls; replaced after per_process
    photos (ONNX Runtime leaks), when it dies, and let go of by close()."""

    def __init__(self, per_process: int, pool_factory: Callable[[], Any] | None = None, *,
                 clock: Callable[[], float] = time.monotonic, poll_sec: float = POLL_SEC,
                 timeout_sec: float = DETECT_TIMEOUT_SEC) -> None:
        self._per_process = max(1, per_process)
        self._pool: Any = None
        self._pool_factory = pool_factory or self._new_pool
        self._clock = clock
        self._poll_sec = poll_sec
        self._timeout_sec = timeout_sec
        self._running = 0
        self._lock = threading.Lock()

    def _new_pool(self) -> Any:
        return mp.get_context("spawn").Pool(1, initializer=silence_stdout, maxtasksperchild=self._per_process)

    @property
    def idle(self) -> bool:
        return self._running == 0

    def close_if_idle(self) -> bool:
        """Let go of the process unless a call is under way (checked and done
        under one lock, so none can start in between). True when let go of."""
        with self._lock:
            if self._running or self._pool is None:
                return False
            pool, self._pool = self._pool, None
        try:
            pool.terminate()
            pool.join()
        except Exception:  # noqa: BLE001
            pass
        return True

    def detect(self, image: bytes, settings: dict) -> dict:
        """What detect_in_child found. Died when the process died or hung (it's
        replaced); Stopped when it was let go of meanwhile."""
        with self._lock:
            self._running += 1
            if self._pool is None:
                self._pool = self._pool_factory()
            pool = self._pool
        try:
            try:
                pending = pool.apply_async(detect_in_child, (image, settings))
                deadline = self._clock() + self._timeout_sec
                while True:
                    try:
                        return pending.get(timeout=self._poll_sec)
                    except mp.TimeoutError:
                        if self._pool is not pool:  # let go of (a stop, idle)
                            raise Stopped("face detection let go of") from None
                        if self._clock() >= deadline:
                            raise
            except Stopped:
                raise
            except Exception as e:  # noqa: BLE001 — the process died or hung
                if self._pool is not pool:  # let go of before it could start
                    raise Stopped("face detection let go of") from None
                logger.exception("scheduler: face detection's process died or hung; a new one takes over")
                self._close(pool)
                raise Died("face detection's process died or hung") from e
        finally:
            with self._lock:
                self._running -= 1

    def close(self) -> bool:
        """Let go of the process (a call under way ends, not tried). True when there was one."""
        return self._close(None)

    def _close(self, only: Any) -> bool:
        with self._lock:
            pool = self._pool
            if pool is None or (only is not None and pool is not only):
                return False
            self._pool = None
        try:
            pool.terminate()
            pool.join()
        except Exception:  # noqa: BLE001
            pass
        return True
