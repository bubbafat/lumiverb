"""AI vision worker. Uses OpenAI-compatible caption provider."""

from __future__ import annotations

import logging
from pathlib import Path

from src.storage.local import LocalStorage
from src.workers.base import BaseWorker
from src.workers.captions.factory import get_caption_provider

logger = logging.getLogger(__name__)


class VisionWorker(BaseWorker):
    job_type = "ai_vision"

    def __init__(self, client: object, storage: LocalStorage, **kwargs: object) -> None:
        super().__init__(client=client, **kwargs)
        self._storage = storage

    def process(self, job: dict) -> dict:
        asset_id = job["asset_id"]
        proxy_key = job.get("proxy_key")
        vision_model_id = job.get("vision_model_id", "")
        vision_api_url = job.get("vision_api_url", "")
        vision_api_key = job.get("vision_api_key") or None

        if not proxy_key:
            raise ValueError(f"No proxy_key in ai_vision job for asset {asset_id}")
        if not vision_api_url:
            raise ValueError(f"No vision_api_url configured for asset {asset_id}")
        if not vision_model_id:
            raise ValueError(f"No vision_model_id configured for asset {asset_id}")

        proxy_path = Path(self._storage.abs_path(proxy_key))

        provider = get_caption_provider(vision_model_id, vision_api_url, vision_api_key)
        result = provider.describe(proxy_path)

        if not result:
            raise RuntimeError(
                f"Caption provider {vision_model_id!r} returned empty result "
                f"for asset {asset_id}"
            )

        description = (result.get("description") or "").strip()
        raw_tags = result.get("tags") or []
        tags = [t.strip() for t in raw_tags if isinstance(t, str) and t.strip()]

        if not description and not tags:
            raise RuntimeError(
                f"Caption provider {vision_model_id!r} returned empty description and tags "
                f"for asset {asset_id}"
            )

        return {
            "model_id": vision_model_id,
            "model_version": "1",
            "description": description,
            "tags": tags,
        }
