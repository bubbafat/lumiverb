"""A video's scenes, found in its analysis proxy (VideoScanner + SceneSegmenter).

The scene window comes from the video's length and the account's Detail
(src/producers/scenes/detail.py). The server hands the video out in chunks
of whole windows (POST /v1/video/{id}/chunks with the window, then
chunks/next and chunks/{id}/complete); each chunk's scenes are saved as it
completes, through the job's client (src/producers/runner.py: it stops
saving once the scheduler stops, and its errors are judged like any
save's). A chunk under way elsewhere (a scheduler stopped mid-chunk holds
it until its lease runs out) makes the video wait, uncharged. When the last completes, the server merges scenes that look alike
and only then counts the video as found. A chunk that fails ends the
video: it's charged, and its chunks are tried again on its next turn.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from threading import Event
from typing import Any

from src.processing.api import LumiverbAPIError
from src.processing.video.scene_segmenter import SceneSegmenter
from src.processing.video.video_scanner import SyncError, VideoScanner
from src.producers.runner import Failed, Stopped, Waits, Work
from src.producers.scenes.detail import FRAME_WIDTH, name_of, window_sec

logger = logging.getLogger(__name__)


class ChunkFailed(RuntimeError):
    """A chunk failed: the video ends, and the chunk stays failed until its next turn."""


def index_video_scenes(*, client: Any, source_path: Path, asset_id: str, duration_sec: float, rel_path: str,
                       lineage: dict, detail: str, redo: bool = False, stopping: Event | None = None) -> dict:
    """Find a video's scenes at a detail, chunk by chunk, and save them as
    each chunk is done. lineage: their record (with the level they're found
    at), sent with every chunk and recorded when the last completes. redo:
    the clip's scenes were found another way; the server starts it over
    (its old scenes and their descriptions go).

    Once stopping is set no more chunks are claimed (Stopped). A chunk that
    fails raises ChunkFailed. Returns {"scenes", "chunks", "elapsed"}."""
    window = window_sec(duration_sec, detail)
    t0 = time.perf_counter()
    total_scenes = total_chunks = 0

    # The chunks, made once (idempotent: safe for a retry).
    init = client.post(f"/v1/video/{asset_id}/chunks", json={
        "duration_sec": duration_sec, "scene_sec": window,
        **({"redo": True, "lineage": lineage} if redo else {})}).json()
    logger.info("video-index: %s — %d chunks, scenes of %.1fs (%s)", rel_path, init["chunk_count"], window,
                "already initialized" if init["already_initialized"] else "created")

    scanner = VideoScanner(source_path, width=FRAME_WIDTH)
    while True:
        if stopping is not None and stopping.is_set():
            raise Stopped(asset_id)
        try:
            claim = client.post(f"/v1/video/{asset_id}/chunks/next")
        except LumiverbAPIError as e:
            if e.code == "chunk_under_way":
                raise Waits(e.message) from e
            raise
        if claim.status_code == 204:
            break  # none left to claim
        work = claim.json()
        chunk_id, worker_id = work["chunk_id"], work["worker_id"]
        try:
            # Just the chunk's own time, [start, end): chunks meet end to start, so no keyframe is in two.
            frames = scanner.scan(work["start_ts"], work["end_ts"])
            scenes = SceneSegmenter(frames, start=work["start_ts"], end=work["end_ts"], window_sec=window,
                                    last=bool(work.get("is_last"))).segment()
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
        self.detail = acct.producers.uses(self.artifact).get("detail")

    def make(self, clip: dict) -> None:
        if not clip.get("duration_sec"):
            raise Waits("no duration yet")
        source = self.acct.analysis_cache.get(clip["asset_id"])
        if source is None or not source.is_file():
            raise Waits("no analysis proxy")
        from src.shared.producers import level_of

        level = level_of(self.artifact, {"detail": self.detail})
        try:
            result = index_video_scenes(client=self.client, source_path=source, asset_id=clip["asset_id"],
                                        duration_sec=clip["duration_sec"], rel_path=clip["rel_path"],
                                        lineage={**self.lineage(clip, used=self.used), "level": level},
                                        detail=name_of(level), redo=bool(clip.get("redo")),
                                        stopping=self.acct.stopping)
        except ChunkFailed as e:
            raise Failed(e) from e
        logger.info("video-index: %s — %d scenes in %d chunks (%.1fs)", clip["rel_path"], result["scenes"],
                    result["chunks"], result["elapsed"])
        return None  # saved chunk by chunk

    def save(self, client, made) -> None:
        pass  # saved as it was made
