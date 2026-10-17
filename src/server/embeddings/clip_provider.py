"""CLIP embedding provider using open-clip-torch."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from src.server.embeddings.base import EmbeddingProvider

logger = logging.getLogger(__name__)

MODEL_ID = "clip"
# Version encodes the model+pretrained combo for provenance
MODEL_VERSION = "ViT-B-32-openai"


class CLIPEmbeddingProvider(EmbeddingProvider):
    """
    Produces 512-dim embeddings using CLIP ViT-B/32 (OpenAI weights).
    Lazy-loads model and preprocessing on first call (thread-safe).
    """

    def __init__(
        self,
        model_name: str = "ViT-B-32",
        pretrained: str = "openai",
    ) -> None:
        self._model_name = model_name
        self._pretrained = pretrained
        self._model = None
        self._preprocess = None
        self._device: str | None = None
        self._lock = threading.Lock()

    @property
    def model_id(self) -> str:
        return MODEL_ID

    @property
    def model_version(self) -> str:
        return f"{self._model_name}-{self._pretrained}"

    def _load(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    # Its weights in the models folder (DATA_DIR/models,
                    # the scheduler's too): the systemd unit makes the
                    # service's home read-only, outside DATA_DIR. Set before
                    # `import open_clip`, which reads it at import time (the
                    # API sets it as it starts too: main.py).
                    from src.processing.models_dir import use_models_dir

                    use_models_dir()

                    import open_clip

                    from src.shared.torch_device import cpu_threads_for, torch_device

                    self._device = torch_device()
                    cpu_threads_for(self._device)
                    model, _, preprocess = open_clip.create_model_and_transforms(
                        self._model_name,
                        pretrained=self._pretrained,
                        device=self._device,
                    )
                    model.eval()
                    self._preprocess = preprocess
                    self._model = model  # publish last so other threads see complete state
                    logger.info(
                        "Loaded CLIP model %s/%s on %s",
                        self._model_name,
                        self._pretrained,
                        self._device,
                    )
        return self._model, self._preprocess, self._device

    def embed_image(self, pil_image: "PIL.Image.Image") -> list[float]:
        """Embed an already-open PIL Image. Shared by embed() and the API endpoint."""
        import numpy as np
        import torch

        from src.shared.torch_device import cpu_threads_for

        model, preprocess, device = self._load()
        cpu_threads_for(device)  # this thread's, whoever built the model
        tensor = preprocess(pil_image).unsqueeze(0).to(device)
        with torch.no_grad():
            features = model.encode_image(tensor)
            features = features / features.norm(dim=-1, keepdim=True)
        vec = features.squeeze(0).cpu().numpy()
        return np.asarray(vec, dtype=float).tolist()

    def embed(self, proxy_path: Path) -> list[float]:
        """NotAnImageError unless the file is an image the server reads (src/server/images.py)."""
        from src.server.images import rgb

        img = rgb(proxy_path.read_bytes())
        try:
            return self.embed_image(img)
        finally:
            img.close()
