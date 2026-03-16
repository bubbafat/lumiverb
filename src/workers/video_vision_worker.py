"""Video vision worker. Describes each scene's representative frame and enqueues search sync."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from src.core.config import get_settings
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
        progress_callback: Callable[[dict], None] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(client=client, once=once, library_id=library_id, **kwargs)
        self._progress_callback = progress_callback

    def _emit(self, event: dict) -> None:
        if self._progress_callback:
            try:
                self._progress_callback(event)
            except Exception:
                pass

    def process(self, job: dict) -> dict:
        """
        Fetch all scenes for the asset, describe each rep frame, update scene vision,
        enqueue scene-level search sync. Returns result dict for complete_job (sets
        video_indexed and enqueues asset-level sync on the server).
        """
        asset_id = job["asset_id"]
        vision_model_id = job.get("vision_model_id", "")
        vision_api_url = job.get("vision_api_url", "")
        vision_api_key = job.get("vision_api_key") or None

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

        provider = get_caption_provider(vision_model_id, vision_api_url, vision_api_key)
        model_version = "1"

        self._emit(
            {
                "event": "job_started",
                "rel_path": job.get("rel_path", ""),
                "total_scenes": len(scenes),
            }
        )

        for scene_idx, scene in enumerate(scenes):
            if scene.get("description"):
                logger.debug(
                    "Scene %s already has description; skipping", scene.get("scene_id")
                )
                continue

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

            self._emit(
                {
                    "event": "scene_started",
                    "rel_path": job.get("rel_path", ""),
                    "scene_index": scene_idx,
                    "total_scenes": len(scenes),
                    "start_ms": scene.get("start_ms", 0),
                    "end_ms": scene.get("end_ms", 0),
                }
            )

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

            self._emit(
                {
                    "event": "scene_complete",
                    "scene_index": scene_idx,
                    "total_scenes": len(scenes),
                }
            )

        resp = self._client.get(f"/v1/video/{asset_id}/scenes")
        resp.raise_for_status()
        refreshed = resp.json()["scenes"]
        missing = [
            s["scene_id"]
            for s in refreshed
            if not s.get("description") and s.get("thumbnail_key")
        ]
        if missing:
            raise RuntimeError(
                f"{len(missing)} scene(s) still missing description after vision pass "
                f"for asset {asset_id}: {missing}"
            )

        return {
            "model_id": vision_model_id,
            "model_version": model_version,
            "description": "",
            "tags": [],
        }
