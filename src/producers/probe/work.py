"""Probing a video: one ffprobe pass over its original, saved as its facet."""

from __future__ import annotations

import logging

from src.producers.runner import Failed, Work

logger = logging.getLogger(__name__)


class Probe(Work):
    artifact = "probe"

    def make(self, clip: dict) -> dict:
        from src.processing.video.probe import probe_video

        source = self.original(clip)
        try:
            return probe_video(source).to_dict()
        except Exception as e:  # noqa: BLE001 — any ffprobe failure is the clip's
            logger.warning("Probe failed for %s: %s", clip["rel_path"], e)
            raise Failed(e) from e

    def save(self, client, made: list[tuple[dict, dict]]) -> None:
        for clip, facet in made:
            client.put(f"/v1/assets/{clip['asset_id']}/video-facet", json={**facet, "lineage": self.lineage(clip)})
