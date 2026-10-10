"""A video's scenes, found in its analysis proxy (VideoScanner + SceneSegmenter).

The server hands a video out in chunks (POST /v1/video/{id}/chunks, then
chunks/next and chunks/{id}/complete), each carrying on from the last
one's anchor, and records the scenes as each completes: so the scenes are
saved as they're made, through the job's client (src/producers/runner.py:
it stops saving once the scheduler stops, and its errors are judged like
any save's). A chunk that fails ends the video: it's charged, and its
chunks are tried again on its next turn.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from pathlib import Path
from threading import Event
from typing import Any

from src.processing.video.scene_segmenter import SceneSegmenter, SceneSettings
from src.processing.video.video_scanner import SyncError, VideoScanner
from src.producers.runner import Failed, Stopped, Waits, Work

logger = logging.getLogger(__name__)


class ChunkFailed(RuntimeError):
    """A chunk failed: the video ends, and the chunk stays failed until its next turn."""


def index_video_scenes(*, client: Any, source_path: Path, asset_id: str, duration_sec: float, rel_path: str,
                       lineage: dict, settings: Mapping[str, Any], redo: bool = False,
                       stopping: Event | None = None) -> dict:
    """Find a video's scenes, chunk by chunk, and save them as each chunk is
    done. settings: how scenes are found (the producer's, as the server says
    them); lineage: their record, sent with every chunk and recorded when the
    last completes. redo: the clip's scenes were found another way; the
    server starts it over (its old scenes and their descriptions go).

    Once stopping is set no more chunks are claimed (Stopped). A chunk that
    fails raises ChunkFailed. Returns {"scenes", "chunks", "elapsed"}."""
    found_with = SceneSettings.for_producer(settings)
    t0 = time.perf_counter()
    total_scenes = total_chunks = 0

    # The chunks, made once (idempotent: safe for a retry).
    init = client.post(f"/v1/video/{asset_id}/chunks", json={
        "duration_sec": duration_sec, **({"redo": True, "lineage": lineage} if redo else {})}).json()
    logger.info("video-index: %s — %d chunks (%s)", rel_path, init["chunk_count"],
                "already initialized" if init["already_initialized"] else "created")

    scanner = VideoScanner(source_path, width=found_with.frame_width)
    while True:
        if stopping is not None and stopping.is_set():
            raise Stopped(asset_id)
        claim = client.post(f"/v1/video/{asset_id}/chunks/next")
        if claim.status_code == 204:
            break  # none left to claim
        work = claim.json()
        chunk_id, worker_id = work["chunk_id"], work["worker_id"]
        try:
            # Scanned with overlap, for the anchor's continuity.
            frames = scanner.scan(max(0.0, work["start_ts"] - work.get("overlap_sec", 2.0)), work["end_ts"])
            segmenter = SceneSegmenter(frames, anchor_phash=work.get("anchor_phash"),
                                       scene_start_ts=work.get("scene_start_ts"), settings=found_with)
            scenes = segmenter.segment()
        except Exception as e:  # noqa: BLE001 — the chunk's: it's let go of as failed
            if isinstance(e, SyncError):
                logger.warning("video-index: %s chunk %d — FFmpeg sync error: %s", rel_path, work["chunk_index"], e)
            else:
                logger.exception("video-index: %s chunk %d — failed: %s", rel_path, work["chunk_index"], e)
            error = str(e) or type(e).__name__
            client.post(f"/v1/video/chunks/{chunk_id}/fail", json={"worker_id": worker_id, "error_message": error})
            # The video isn't indexed until every chunk is: it's tried again once its turn comes.
            raise ChunkFailed(f"chunk {work['chunk_index']} failed: {error}") from e

        # Saving it failing isn't the chunk's fault: the error goes on as it
        # is, and the chunk is let go of (failed) so the next turn starts from it.
        try:
            client.post(f"/v1/video/chunks/{chunk_id}/complete", json={
                "worker_id": worker_id,
                "scenes": [{"start_ms": s.start_ms, "end_ms": s.end_ms, "rep_frame_ms": s.rep_frame_ms,
                            "sharpness_score": s.sharpness_score, "keep_reason": s.keep_reason, "phash": s.phash}
                           for s in scenes],
                "next_anchor_phash": segmenter.next_anchor_phash,
                "next_scene_start_ms": segmenter.next_scene_start_ms,
                "lineage": lineage,
            })
        except Exception as e:
            if not isinstance(e, Stopped):
                try:
                    client.post(f"/v1/video/chunks/{chunk_id}/fail",
                                json={"worker_id": worker_id, "error_message": str(e) or type(e).__name__})
                except Exception:  # noqa: BLE001 — its lease runs out instead
                    logger.warning("video-index: %s chunk %d — couldn't let go of it", rel_path, work["chunk_index"])
            raise
        total_scenes += len(scenes)
        total_chunks += 1
        logger.info("video-index: %s chunk %d — %d scenes", rel_path, work["chunk_index"], len(scenes))
    return {"scenes": total_scenes, "chunks": total_chunks, "elapsed": time.perf_counter() - t0}


class Scenes(Work):
    artifact = "scenes"

    def __init__(self, acct, job) -> None:
        super().__init__(acct, job)
        # One read for the job: what's found and what its lineage says agree.
        self.used = acct.producers.settings(self.artifact)

    def make(self, clip: dict) -> None:
        if not clip.get("duration_sec"):
            raise Waits("no duration yet")
        source = self.acct.analysis_cache.get(clip["asset_id"])
        if source is None or not source.is_file():
            raise Waits("no analysis proxy")
        try:
            result = index_video_scenes(client=self.client, source_path=source, asset_id=clip["asset_id"],
                                        duration_sec=clip["duration_sec"], rel_path=clip["rel_path"],
                                        lineage=self.lineage(clip, used=self.used), settings=self.used,
                                        redo=bool(clip.get("redo")), stopping=self.acct.stopping)
        except ChunkFailed as e:
            raise Failed(e) from e
        logger.info("video-index: %s — %d scenes in %d chunks (%.1fs)", clip["rel_path"], result["scenes"],
                    result["chunks"], result["elapsed"])
        return None  # saved chunk by chunk

    def save(self, client, made) -> None:
        pass  # saved as it was made
