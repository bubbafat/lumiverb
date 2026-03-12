"""Embedding worker: generates CLIP and Moondream vectors for similarity search."""

from __future__ import annotations

import logging
from pathlib import Path

from src.models.registry import get_embedding_config
from src.storage.local import LocalStorage
from src.workers.base import BaseWorker
from src.workers.embeddings.clip_provider import CLIPEmbeddingProvider, MODEL_VERSION as CLIP_VERSION
from src.workers.embeddings.moondream_provider import MoondreamEmbeddingProvider, MODEL_VERSION as MD_VERSION

logger = logging.getLogger(__name__)


class EmbedWorker(BaseWorker):
    job_type = "embed"

    def __init__(self, client: object, storage: LocalStorage, **kwargs: object) -> None:
        super().__init__(client=client, **kwargs)
        self._storage = storage
        self._clip: CLIPEmbeddingProvider | None = None
        self._moondream: MoondreamEmbeddingProvider | None = None

    def _get_clip(self) -> CLIPEmbeddingProvider:
        if self._clip is None:
            self._clip = CLIPEmbeddingProvider()
        return self._clip

    def _get_moondream(self) -> MoondreamEmbeddingProvider:
        if self._moondream is None:
            self._moondream = MoondreamEmbeddingProvider()
        return self._moondream

    def process(self, job: dict) -> dict:
        asset_id = job["asset_id"]
        proxy_key = job.get("proxy_key")
        vision_model_id = job.get("vision_model_id", "moondream")

        if not proxy_key:
            raise ValueError(f"No proxy_key in embed job for asset {asset_id}")

        proxy_path = Path(self._storage.abs_path(proxy_key))
        if not proxy_path.exists():
            raise FileNotFoundError(f"Proxy file not found: {proxy_path}")

        config = get_embedding_config(vision_model_id)
        embeddings: list[dict] = []

        clip_provider = self._get_clip()
        clip_vec = clip_provider.embed(proxy_path)
        embeddings.append({
            "model_id": clip_provider.model_id,
            "model_version": CLIP_VERSION,
            "vector": clip_vec,
        })

        if config.moondream_weight > 0:
            md_provider = self._get_moondream()
            md_vec = md_provider.embed(proxy_path)
            embeddings.append({
                "model_id": md_provider.model_id,
                "model_version": MD_VERSION,
                "vector": md_vec,
            })

        return {"embeddings": embeddings}
