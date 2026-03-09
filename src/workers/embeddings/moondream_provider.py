"""Moondream embedding provider. Uses encode_image() from moondream SDK."""

from __future__ import annotations

import logging
from pathlib import Path

from src.workers.embeddings.base import EmbeddingProvider

logger = logging.getLogger(__name__)

MODEL_ID = "moondream"
MODEL_VERSION = "2"


class MoondreamEmbeddingProvider(EmbeddingProvider):
    """
    Produces 512-dim embeddings using Moondream's image encoder.
    Lazy-loads the model on first call (same process as VisionWorker).
    """

    def __init__(self) -> None:
        self._model = None

    @property
    def model_id(self) -> str:
        return MODEL_ID

    @property
    def model_version(self) -> str:
        return MODEL_VERSION

    def _load(self):
        if self._model is None:
            import moondream as md

            self._model = md.vl(model="moondream-2b-int8.mf")
        return self._model

    def embed(self, proxy_path: Path) -> list[float]:
        from PIL import Image as PILImage
        import numpy as np

        model = self._load()
        image = PILImage.open(proxy_path)
        # encode_image returns a tensor/array; convert to Python list[float]
        encoded = model.encode_image(image)
        # Flatten to 1D and take first 512 dims (or pad if needed)
        vec = np.array(encoded).flatten()
        if len(vec) > 512:
            vec = vec[:512]
        elif len(vec) < 512:
            vec = np.pad(vec, (0, 512 - len(vec)))
        # L2-normalize for cosine distance consistency
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec.tolist()

