"""Video index worker.

Claims `video-index` jobs, transcodes the source video to a 720p proxy in a temp dir,
and processes the asset in 30-second chunks using the video chunk API:

- POST /v1/video/{asset_id}/chunks          (init chunks)
- GET  /v1/video/{asset_id}/chunks/next    (claim next chunk; 204 when done)
- POST /v1/video/chunks/{chunk_id}/complete
- POST /v1/video/chunks/{chunk_id}/fail

Scene segmentation is performed locally using VideoScanner + SceneSegmenter,
and high-resolution representative frames are extracted from the full source
via FFmpeg into LocalStorage using scene_rep_key().
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from src.core.config import get_settings
from src.storage.local import LocalStorage
from src.video.clip_extractor import (
    extract_video_frame,
    probe_video_duration,
    transcode_to_720p_h264,
)
from src.video.scene_segmenter import SceneSegmenter
from src.video.video_scanner import VideoScanner
from src.workers.base import BaseWorker

logger = logging.getLogger(__name__)


class VideoIndexWorker(BaseWorker):
    job_type = "video-index"

    def __init__(
        self,
        client: object,
        once: bool = False,
        library_id: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(client=client, once=once, library_id=library_id, **kwargs)

    def process(self, job: dict) -> dict:
        """Process a single video-index job. Returns {}; chunk work is done via video API."""
        asset_id = job["asset_id"]
        source = Path(job["root_path"]) / job["rel_path"]
        if not source.exists():
            raise FileNotFoundError(f"Source not found: {source}")

        duration_sec = job.get("duration_sec") or None
        if not duration_sec:
            duration_sec = probe_video_duration(source)
        if not duration_sec:
            raise ValueError(f"Could not determine duration for {source}")

        # Init chunks (idempotent)
        resp = self._request("POST", f"/v1/video/{asset_id}/chunks", json={"duration_sec": duration_sec})
        if resp.status_code // 100 != 2:
            raise RuntimeError(
                f"Failed to init video chunks for asset {asset_id}: HTTP {resp.status_code} {resp.text}"
            )

        settings = get_settings()
        storage = LocalStorage(data_dir=settings.data_dir)
        tenant_ctx = self._request("GET", "/v1/tenant/context").json()
        tenant_id = tenant_ctx["tenant_id"]
        library_id = job["library_id"]

        tmpdir = Path(tempfile.mkdtemp())
        try:
            proxy_path = tmpdir / f"{asset_id}_proxy.mp4"
            ok = transcode_to_720p_h264(source, proxy_path)
            if not ok:
                raise RuntimeError(f"Transcode failed for {source}")

            # Thumbnail: extract frame at 0, write to storage, then record thumbnail_key on asset.
            thumb_path = tmpdir / f"{asset_id}_thumb.jpg"
            if extract_video_frame(source, thumb_path, timestamp=0.0) and thumb_path.exists():
                original_filename = Path(job["rel_path"]).name
                key = storage.thumbnail_key(tenant_id, library_id, asset_id, original_filename)
                dest = storage.abs_path(key)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(thumb_path, dest)
                resp = self._request("POST", f"/v1/assets/{asset_id}/thumbnail-key", json={"thumbnail_key": key})
                if resp.status_code // 100 != 2:
                    logger.warning(
                        "Failed to record thumbnail_key for asset %s: HTTP %s",
                        asset_id, resp.status_code,
                    )

            while True:
                resp = self._request("GET", f"/v1/video/{asset_id}/chunks/next")
                if resp.status_code == 204:
                    break
                resp.raise_for_status()
                work_order = resp.json()
                self._process_chunk(
                    source=source,
                    proxy_path=proxy_path,
                    work_order=work_order,
                    tmpdir=tmpdir,
                    storage=storage,
                    tenant_id=tenant_id,
                    library_id=library_id,
                    asset_id=asset_id,
                )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        return {}

    def _request(self, method: str, path: str, **kwargs: object):
        """Raw request for endpoints that use 204/409; returns response without exiting."""
        return self._client.raw(method, path, **kwargs)

    def _process_chunk(
        self,
        source: Path,
        proxy_path: Path,
        work_order: dict,
        tmpdir: Path,
        storage: LocalStorage,
        tenant_id: str,
        library_id: str,
        asset_id: str,
    ) -> None:
        """Run scene detection on chunk, extract rep frames, complete or fail chunk."""
        chunk_id = work_order["chunk_id"]
        worker_id = work_order["worker_id"]
        scan_start = max(0.0, work_order["start_ts"] - work_order["overlap_sec"])
        scan_end = work_order["end_ts"]

        try:
            scanner = VideoScanner(proxy_path)
            raw_frames = scanner.scan(start_ts=scan_start, end_ts=scan_end)
            segmenter = SceneSegmenter(
                frames=raw_frames,
                anchor_phash=work_order.get("anchor_phash"),
                scene_start_ts=work_order.get("scene_start_ts"),
            )
            scenes_raw = segmenter.segment()

            start_boundary_ms = int(work_order["start_ts"] * 1000)
            filtered = [s for s in scenes_raw if s.end_ms > start_boundary_ms]

            scene_dicts = []
            for i, scene in enumerate(filtered):
                rep_path = tmpdir / f"{asset_id}_{scene.rep_frame_ms}.jpg"
                extract_video_frame(source, rep_path, timestamp=scene.rep_frame_ms / 1000.0)
                key = storage.scene_rep_key(
                    tenant_id=tenant_id,
                    library_id=library_id,
                    asset_id=asset_id,
                    rep_frame_ms=scene.rep_frame_ms,
                )
                dest = storage.abs_path(key)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(rep_path, dest)
                rep_path.unlink(missing_ok=True)
                scene_dicts.append({
                    "scene_index": i,
                    "start_ms": scene.start_ms,
                    "end_ms": scene.end_ms,
                    "rep_frame_ms": scene.rep_frame_ms,
                    "proxy_key": None,
                    "thumbnail_key": key,
                    "description": None,
                    "tags": None,
                    "sharpness_score": getattr(scene, "sharpness_score", None),
                    "keep_reason": str(scene.keep_reason) if getattr(scene, "keep_reason", None) else None,
                    "phash": getattr(scene, "phash", None),
                })

            next_anchor_phash = getattr(segmenter, "next_anchor_phash", None)
            next_scene_start_ms = getattr(segmenter, "next_scene_start_ms", None)
            if next_anchor_phash is None and scenes_raw:
                next_anchor_phash = getattr(scenes_raw[-1], "phash", None)

            resp = self._request(
                "POST",
                f"/v1/video/chunks/{chunk_id}/complete",
                json={
                    "worker_id": worker_id,
                    "scenes": scene_dicts,
                    "next_anchor_phash": next_anchor_phash,
                    "next_scene_start_ms": next_scene_start_ms,
                },
            )
            if resp.status_code == 409:
                logger.warning("Chunk %s lease expired; skipping", chunk_id)
                return
            resp.raise_for_status()
        except Exception as e:
            self._request(
                "POST",
                f"/v1/video/chunks/{chunk_id}/fail",
                json={"worker_id": worker_id, "error_message": str(e)},
            )
            raise
