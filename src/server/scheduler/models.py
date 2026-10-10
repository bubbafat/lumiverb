"""The GPU models the scheduler holds: one of each, shared by every account
(ADR-016 phase 4).

Nothing in a model is an account's: CLIP turns an image into a vector, and
the face process finds faces in an image; each account saves what's made
through its own API client. So a model is loaded once for the scheduler,
keyed by the settings it's loaded with (CLIP's model and weights; the face
process takes each photo's settings), and every account's jobs use it. A
model unused for IDLE_SEC is let go of, so the GPU is free for the rest
(the AI machines that may share it).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# A model unused this long is let go of (its GPU memory with it).
IDLE_SEC = 600.0


class Models:
    def __init__(self, *, face_batches_per_process: int = 20, batch: int | None = None,
                 clip_factory: Callable[[str, str], Any] | None = None,
                 face_factory: Callable[[], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        from src.shared.producers import PRODUCERS

        # The face process does so many jobs' photos before a fresh one takes over.
        self._face_clips = max(1, face_batches_per_process) * (batch or PRODUCERS["faces"].batch)
        self._clip_factory = clip_factory or _clip
        self._face_factory = face_factory or self._face_detector
        self._clock = clock
        self._lock = threading.Lock()
        self._clip: tuple[tuple[str, str], Any] | None = None
        self._faces: Any = None
        self._used: dict[str, float] = {}

    def _face_detector(self) -> Any:
        from src.producers.faces.detect import FaceDetector

        return FaceDetector(self._face_clips)

    def clip(self, model: str, pretrained: str) -> Any:
        """CLIP loaded with these settings (loaded again only when they change)."""
        key = (model, pretrained)
        with self._lock:
            if self._clip is None or self._clip[0] != key:
                if self._clip is not None:
                    logger.info("scheduler: CLIP's settings changed; loading %s/%s", model, pretrained)
                self._clip = (key, self._clip_factory(model, pretrained))
            self._used["clip"] = self._clock()
            return self._clip[1]

    def faces(self) -> Any:
        """The face detection process (src/producers/faces/detect.py)."""
        with self._lock:
            if self._faces is None:
                self._faces = self._face_factory()
            self._used["faces"] = self._clock()
            return self._faces

    def let_go_of_idle(self) -> None:
        """Let go of what's been unused for IDLE_SEC."""
        now = self._clock()
        with self._lock:
            clip = self._clip if self._clip is not None and now - self._used.get("clip", now) >= IDLE_SEC else None
            if clip is not None:
                self._clip = None
            faces = self._faces
            idle_faces = faces is not None and now - self._used.get("faces", now) >= IDLE_SEC and faces.idle
        if clip is not None:
            del clip
            _free_gpu_memory()
            logger.info("scheduler: let go of the CLIP model (unused)")
        if idle_faces and faces.close():
            logger.info("scheduler: let go of the face detection process (unused)")

    def close(self) -> None:
        """Let go of everything (a call under way ends, not tried)."""
        with self._lock:
            faces, self._faces, self._clip = self._faces, None, None
        if faces is not None:
            faces.close()


def _clip(model: str, pretrained: str) -> Any:
    from src.processing.workers.embeddings.clip_provider import CLIPEmbeddingProvider

    return CLIPEmbeddingProvider(model_name=model, pretrained=pretrained)


def _free_gpu_memory() -> None:
    import gc

    gc.collect()  # the model's tensors, held by reference cycles, before the cache is emptied
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001 — no torch, or no GPU
        pass
