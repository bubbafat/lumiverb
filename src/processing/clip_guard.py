"""CLIP vectors only while a machine doing them offers the account's model (ADR-022).

The CLIP job's machines: the built-in one (the scheduler's own CLIP, on this
machine's GPU, as it has always made them: src/server/scheduler/models.py)
and Lumiverb's AI workers (lumiverb-server ai-worker). See
src/processing/job_guard.py for when its work goes ahead, stops, and when a
clip is charged a failure.
"""

from __future__ import annotations

import importlib.util
import io
import time
from collections.abc import Callable
from typing import Any

from src.processing.ai_pool import Machine, PooledEmbedder
from src.processing.job_guard import JobGuard
from src.processing.workers.embeddings.base import EmbeddingError
from src.producers.clip import MODELS


def built_in_clip(model: str) -> tuple[list[str], str, str]:
    """(the models the built-in machine offers, the one it serves, why it can't): CLIP here."""
    if importlib.util.find_spec("open_clip") is None:
        return [], "", "This computer can't make CLIP vectors: open-clip-torch isn't installed."
    if model not in MODELS:
        return list(MODELS), "", f"The built-in CLIP doesn't know {model}."
    return list(MODELS), model, ""


class BuiltInClip:
    """The scheduler's own CLIP: one image at a time on this GPU, taking turns with face detection."""

    def __init__(self, models: Any, model: str) -> None:
        self._models = models
        self._model = model

    def embed(self, image: bytes) -> dict[str, Any]:
        from PIL import Image

        from src.producers.runner import out_of_memory

        try:
            img = Image.open(io.BytesIO(image)).convert("RGB")
        except Exception as e:  # noqa: BLE001 — a preview that can't be read is the clip's (PIL's errors vary)
            raise EmbeddingError(f"its proxy can't be read: {e}", endpoint_fault=False) from e
        try:
            with self._models.gpu_turn():
                provider = self._models.clip(*MODELS[self._model])
                vector = provider.embed_image(img)
        except Exception as e:  # noqa: BLE001 — the model or the GPU: this machine's
            raise EmbeddingError(f"Out of memory: {e}" if out_of_memory(e) else str(e) or type(e).__name__,
                                 endpoint_fault=True) from e
        finally:
            img.close()
        return {"vector": vector, "model_id": provider.model_id, "model_version": provider.model_version}


class WorkerClip:
    """A Lumiverb AI worker's CLIP, asked for the model by the name it lists it as."""

    def __init__(self, machine: Machine, model: str) -> None:
        from src.processing.ai_worker_client import AiWorker

        self._worker = AiWorker(machine.api_url, machine.api_key, fingerprint=machine.fingerprint)
        self._model = model

    def embed(self, image: bytes) -> dict[str, Any]:
        return self._worker.clip(image, self._model)


class ClipGuard(JobGuard):
    def __init__(self, client: Any, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(client, "clip", clock, built_in_check=built_in_clip)

    def embedder(self, models: Any) -> PooledEmbedder:
        """Makes CLIP vectors on whichever online machine is free: models is the
        scheduler's (src/server/scheduler/models.py), the built-in machine's."""
        return PooledEmbedder(self.pool, lambda m: (BuiltInClip(models, m.serves or self.pool.model) if m.built_in
                                                     else WorkerClip(m, m.serves or self.pool.model)))
