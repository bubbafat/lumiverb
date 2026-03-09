"""AI vision worker. Uses Moondream to generate descriptions and tags."""

from __future__ import annotations

import logging

from src.storage.local import LocalStorage
from src.workers.base import BaseWorker
from src.workers.vision import VISION_MODEL_ID, VISION_MODEL_VERSION, describe_image

logger = logging.getLogger(__name__)


class VisionWorker(BaseWorker):
    job_type = "ai_vision"

    def __init__(self, client: object, storage: LocalStorage, **kwargs: object) -> None:
        super().__init__(client=client, **kwargs)
        self._storage = storage

    def process(self, job: dict) -> dict:
        asset_id = job["asset_id"]
        proxy_key = job.get("proxy_key")

        if not proxy_key:
            raise ValueError(f"Asset {asset_id} has no proxy — run proxy worker first")

        proxy_path = self._storage.abs_path(proxy_key)
        if not proxy_path.exists():
            raise FileNotFoundError(f"Proxy not found on disk: {proxy_path}")

        result = describe_image(proxy_path)
        if not result:
            raise RuntimeError(f"Vision inference returned empty result for {asset_id}")

        return {
            "model_id": VISION_MODEL_ID,
            "model_version": VISION_MODEL_VERSION,
            "description": result.get("description", ""),
            "tags": result.get("tags", []),
        }

