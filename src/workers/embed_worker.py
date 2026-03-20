"""Embedding worker: generates CLIP vectors for similarity search."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from src.storage.artifact_store import ArtifactStore
from src.workers.base import BaseWorker, BlockJob
from src.workers.embeddings.clip_provider import CLIPEmbeddingProvider, MODEL_VERSION as CLIP_VERSION

logger = logging.getLogger(__name__)


class EmbedWorker(BaseWorker):
    job_type = "embed"

    def __init__(
        self,
        client: object,
        artifact_store: ArtifactStore,
        **kwargs: object,
    ) -> None:
        super().__init__(client=client, **kwargs)
        self._artifact_store = artifact_store
        self._clip: CLIPEmbeddingProvider | None = None

    def _get_clip(self) -> CLIPEmbeddingProvider:
        if self._clip is None:
            self._clip = CLIPEmbeddingProvider()
        return self._clip

    def process(self, job: dict) -> dict:
        asset_id = job["asset_id"]
        media_type = job.get("media_type", "")
        if not media_type.startswith("image"):
            raise BlockJob(f"embed requires an image asset; got media_type={media_type!r} for asset {asset_id}")
        proxy_key = job.get("proxy_key")

        if not proxy_key:
            raise BlockJob(f"No proxy_key for asset {asset_id} — proxy must complete before embed can run")

        try:
            proxy_bytes = self._artifact_store.read_artifact(
                proxy_key, asset_id=asset_id, artifact_type="proxy"
            )
        except Exception as e:
            raise BlockJob(f"Could not read proxy for asset {asset_id}: {e}") from e

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(proxy_bytes)
            tmp_path = Path(tmp.name)

        clip_provider = self._get_clip()
        try:
            clip_vec = clip_provider.embed(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

        return {
            "embeddings": [
                {
                    "model_id": clip_provider.model_id,
                    "model_version": CLIP_VERSION,
                    "vector": clip_vec,
                }
            ]
        }
