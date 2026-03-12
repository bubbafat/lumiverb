"""Video vision worker. Describes each scene's representative frame and enqueues search sync."""

from __future__ import annotations

import logging
from pathlib import Path

from src.core.config import get_settings
from src.models.registry import model_version_for_provenance
from src.storage.local import LocalStorage
from src.workers.base import BaseWorker
from src.workers.captions.factory import get_caption_provider

logger = logging.getLogger(__name__)


class VideoVisionWorker(BaseWorker):
    job_type = "video-vision"

    def __init__(
        self,
        client: object,
        once: bool = False,
        library_id: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(client=client, once=once, library_id=library_id, **kwargs)

    def process(self, job: dict) -> dict:
        """
        Fetch all scenes for the asset, describe each rep frame, update scene vision,
        enqueue scene-level search sync. Returns result dict for complete_job (sets
        video_indexed and enqueues asset-level sync on the server).
        """
        asset_id = job["asset_id"]
        vision_model_id = job.get("vision_model_id", "moondream")

        resp = self._client.get(f"/v1/video/{asset_id}/scenes")
        resp.raise_for_status()
        scenes = resp.json()["scenes"]

        if not scenes:
            logger.info("No scenes for asset_id=%s; completing immediately", asset_id)
            return {
                "model_id": vision_model_id,
                "model_version": "",
                "description": "",
                "tags": [],
            }

        settings = get_settings()
        storage = LocalStorage(data_dir=settings.data_dir)
        tenant_ctx = self._client.get("/v1/tenant/context").json()
        tenant_id = tenant_ctx["tenant_id"]
        library_id = job["library_id"]

        provider = get_caption_provider(vision_model_id)
        model_version = model_version_for_provenance(vision_model_id)

        for scene in scenes:
            scene_id = scene["scene_id"]
            thumbnail_key = scene.get("thumbnail_key")
            if not thumbnail_key:
                logger.warning("Scene %s has no thumbnail_key; skipping vision", scene_id)
                continue

            rep_path = Path(storage.abs_path(thumbnail_key))
            if not rep_path.exists():
                logger.warning(
                    "Rep frame not found at %s for scene %s; skipping",
                    rep_path,
                    scene_id,
                )
                continue

            result = provider.describe(rep_path)
            if not result:
                logger.warning("Caption provider returned empty result for scene %s", scene_id)
                continue

            description = result.get("description", "")
            tags = result.get("tags", [])

            patch_resp = self._client.patch(
                f"/v1/video/scenes/{scene_id}",
                json={
                    "model_id": vision_model_id,
                    "model_version": model_version,
                    "description": description,
                    "tags": tags,
                },
            )
            patch_resp.raise_for_status()

            self._client.post(
                f"/v1/video/scenes/{scene_id}/sync",
                json={"asset_id": asset_id},
            )

        return {
            "model_id": vision_model_id,
            "model_version": model_version,
            "description": "",
            "tags": [],
        }
