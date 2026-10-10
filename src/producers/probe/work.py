"""Probing a video: one ffprobe pass over its original, saved as its facet."""

from __future__ import annotations

import logging

from src.producers.runner import Failed, Work

logger = logging.getLogger(__name__)


class Probe(Work):
    artifact = "probe"

    def make(self, clip: dict) -> dict:
        import subprocess

        from src.processing.video.probe import probe_video

        source = self.original(clip)
        try:
            return probe_video(source).to_dict()
        # ffprobe refusing the file, or output that can't be read, is the clip's.
        # Anything else (OSError: no ffprobe, the mount's EIO) is a crash: counted, not charged.
        except (subprocess.CalledProcessError, ValueError, TypeError, AttributeError) as e:
            logger.warning("Probe failed for %s: %s", clip["rel_path"], e)
            raise Failed(e) from e

    def save(self, client, made: list[tuple[dict, dict]]) -> None:
        for clip, facet in made:
            client.put(f"/v1/assets/{clip['asset_id']}/video-facet", json={**facet, "lineage": self.lineage(clip)})
