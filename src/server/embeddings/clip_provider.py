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
                    # Force open_clip / huggingface_hub to cache weights in
                    # a writable directory under the configured data dir.
                    # The systemd unit pins the service home filesystem
                    # read-only outside of `ReadWritePaths=${DATA_DIR}`,
                    # so the default `~/.cache/huggingface` location 500s
                    # with "Read-only file system". Set the env vars before
                    # `import open_clip` so they're picked up at module
                    # import time. Idempotent — only sets if not already
                    # configured by the operator.
                    import os
                    from src.server.config import get_settings

                    cache_root = os.path.join(get_settings().data_dir, "hf-cache")
                    os.makedirs(cache_root, exist_ok=True)
                    os.environ.setdefault("HF_HOME", cache_root)
                    os.environ.setdefault("TRANSFORMERS_CACHE", cache_root)
                    os.environ.setdefault("HF_HUB_CACHE", cache_root)

                    import open_clip
                    import torch

                    self._device = "cuda" if torch.cuda.is_available() else "cpu"
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

        model, preprocess, device = self._load()
        tensor = preprocess(pil_image).unsqueeze(0).to(device)
        with torch.no_grad():
            features = model.encode_image(tensor)
            features = features / features.norm(dim=-1, keepdim=True)
        vec = features.squeeze(0).cpu().numpy()
        return np.asarray(vec, dtype=float).tolist()

    def embed(self, proxy_path: Path) -> list[float]:
        from PIL import Image as PILImage

        img = PILImage.open(proxy_path).convert("RGB")
        try:
            return self.embed_image(img)
        finally:
            img.close()
