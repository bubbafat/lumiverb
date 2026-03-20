"""Video vision worker. Describes each scene's representative frame and enqueues search sync."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Callable

from src.storage.artifact_store import ArtifactStore
from src.workers.base import BaseWorker, BlockJob
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
        artifact_store: ArtifactStore | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(client=client, once=once, library_id=library_id, **kwargs)
        self._progress_callback = progress_callback
        self._artifact_store = artifact_store

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
        if job.get("media_type") != "video":
            raise BlockJob(f"video-vision requires a video asset; got media_type={job.get('media_type')!r} for asset {asset_id}")
        vision_model_id = job.get("vision_model_id", "")
        vision_api_url = job.get("vision_api_url", "")
        vision_api_key = job.get("vision_api_key") or None

        resp = self._client.get(f"/v1/video/{asset_id}/scenes")
        resp.raise_for_status()
        scenes = resp.json()["scenes"]

        if not scenes:
            logger.warning(
                "No scenes for asset_id=%s; video-vision job should not have been enqueued. "
                "Completing without work.",
                asset_id,
            )
            return {
                "model_id": vision_model_id,
                "model_version": "",
                "description": "",
                "tags": [],
            }

        if self._artifact_store is None:
            raise ValueError("artifact_store is required for VideoVisionWorker")

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

            rep_frame_ms = scene.get("rep_frame_ms")
            if rep_frame_ms is None:
                logger.warning("Scene %s has no rep_frame_ms; skipping vision", scene_id)
                continue

            try:
                rep_bytes = self._artifact_store.read_artifact(
                    thumbnail_key,
                    asset_id=asset_id,
                    artifact_type="scene_rep",
                    rep_frame_ms=rep_frame_ms,
                )
            except Exception:
                logger.warning(
                    "Rep frame unavailable for scene %s (asset %s); skipping",
                    scene_id,
                    asset_id,
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

            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmpf:
                tmpf.write(rep_bytes)
                tmp_path = Path(tmpf.name)
            try:
                result = provider.describe(tmp_path)
            finally:
                tmp_path.unlink(missing_ok=True)
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
