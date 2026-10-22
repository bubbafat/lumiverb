"""Face detection only while a machine doing it offers the account's model (ADR-022).

The faces job's machines: the built-in one (the scheduler's own face
process, on this machine's GPU, as faces have always been found:
src/server/scheduler/models.py, src/producers/faces/detect.py) and
Lumiverb's AI workers (lumiverb-server ai-worker). Photos' faces and videos'
frames alike. See src/processing/job_guard.py for when its work goes ahead,
stops, and when a clip is charged a failure.
"""

from __future__ import annotations

import importlib.util
import time
from collections.abc import Callable
from typing import Any

from src.processing.ai_pool import Machine, PooledFaces
from src.processing.job_guard import JobGuard
from src.processing.workers.faces import FaceError
from src.producers.faces import MODELS


def built_in_faces(model: str) -> tuple[list[str], str, str]:
    """(the models the built-in machine offers, the one it serves, why it can't): InsightFace here."""
    if importlib.util.find_spec("insightface") is None:
        return [], "", "This computer can't find faces: insightface isn't installed."
    if model not in MODELS:
        return sorted(MODELS), "", f"The built-in face detection doesn't know {model}."
    return sorted(MODELS), model, ""


class BuiltInFaces:
    """The scheduler's own face process: one image at a time on this GPU, taking turns with CLIP."""

    def __init__(self, models: Any) -> None:
        self._models = models

    def detect(self, image: bytes, settings: dict) -> dict:
        from src.producers.runner import out_of_memory

        with self._models.gpu_turn():
            found = self._models.faces().detect(image, settings)  # in use: not let go of meanwhile
        if "error" in found:
            # The GPU's trouble is this machine's; anything else, the image's (one it couldn't read).
            raise FaceError(str(found["error"]), endpoint_fault=out_of_memory(found["error"]))
        return found


class WorkerFaces:
    """A Lumiverb AI worker's face detection."""

    def __init__(self, machine: Machine) -> None:
        from src.processing.ai_worker_client import AiWorker

        self._worker = AiWorker(machine.api_url, machine.api_key, fingerprint=machine.fingerprint, error=FaceError)

    def detect(self, image: bytes, settings: dict) -> dict:
        return self._worker.faces(image, settings)


class FacesGuard(JobGuard):
    def __init__(self, client: Any, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(client, "faces", clock, built_in_check=built_in_faces)

    def detector(self, models: Any) -> PooledFaces:
        """Finds faces on whichever online machine is free: models is the
        scheduler's (src/server/scheduler/models.py), the built-in machine's."""
        return PooledFaces(self.pool, lambda m: BuiltInFaces(models) if m.built_in else WorkerFaces(m))
