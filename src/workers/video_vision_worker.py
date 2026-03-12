"""Video vision worker stub. Marks video-vision jobs complete without work.

CLI wiring and actual scene-level vision will be added in a later step.
"""

from __future__ import annotations

import logging

from src.workers.base import BaseWorker

logger = logging.getLogger(__name__)


class VideoVisionWorker(BaseWorker):
    job_type = "video-vision"

    def process(self, job: dict) -> dict | None:
        """
        Stub implementation: immediately completes the job with an empty result.

        BaseWorker.run() will call complete_job(job_id, result or {}), so returning
        an empty dict here results in an empty payload for video-vision jobs, which
        is what the API expects (it only needs to mark the asset video_indexed and
        enqueue search sync).
        """
        logger.info(
            "video-vision stub: completing job_id=%s asset_id=%s",
            job.get("job_id"),
            job.get("asset_id"),
        )
        return {}

