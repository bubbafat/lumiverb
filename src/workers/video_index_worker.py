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
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from PIL import Image
from src.core.config import get_settings
from src.storage.local import LocalStorage
from src.video.clip_extractor import (
    extract_video_frame,
    probe_video_duration,
)
from src.video.scene_segmenter import DEBOUNCE_SEC, SceneSegmenter
from src.video.video_scanner import RawFrame, VideoScanner
from src.workers.base import BaseWorker

logger = logging.getLogger(__name__)


class VideoIndexWorker(BaseWorker):
    job_type = "video-index"

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

    def process(self, job: dict) -> dict:
        """Process a single video-index job. Returns {}; chunk work is done via video API."""
        asset_id = job["asset_id"]
        root = Path(job["root_path"]).resolve()
        source = (root / job["rel_path"]).resolve()
        if not source.is_relative_to(root):
            raise ValueError(f"rel_path escapes library root: {job['rel_path']!r}")
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

                self._emit(
                    {
                        "event": "chunk_claimed",
                        "rel_path": job["rel_path"],
                        "chunk_index": work_order["chunk_index"],
                        "start_ts": work_order["start_ts"],
                        "end_ts": work_order["end_ts"],
                        "video_duration_sec": duration_sec,
                    }
                )

                chunk_start = max(0.0, work_order["start_ts"] - work_order["overlap_sec"])
                chunk_end = work_order["end_ts"]
                chunk_duration = chunk_end - chunk_start

                proxy_path = tmpdir / f"{asset_id}_chunk_{work_order['chunk_index']}_proxy.mp4"
                ok = self._transcode_chunk_proxy(
                    source=source,
                    dest=proxy_path,
                    start_sec=chunk_start,
                    duration_sec=chunk_duration,
                )
                if not ok:
                    self._emit(
                        {
                            "event": "chunk_failed",
                            "chunk_index": work_order["chunk_index"],
                        }
                    )
                    self._request(
                        "POST",
                        f"/v1/video/chunks/{work_order['chunk_id']}/fail",
                        json={
                            "worker_id": work_order["worker_id"],
                            "error_message": "Proxy transcode failed",
                        },
                    )
                    continue

                try:
                    self._process_chunk(
                        source=source,
                        proxy_path=proxy_path,
                        work_order=work_order,
                        chunk_offset=chunk_start,
                        tmpdir=tmpdir,
                        storage=storage,
                        tenant_id=tenant_id,
                        library_id=library_id,
                        asset_id=asset_id,
                        frame_callback=lambda pts, s, e, _dur=duration_sec: self._emit(
                            {
                                "event": "frame_scanned",
                                "rel_path": job["rel_path"],
                                "pts": pts,
                                "start_ts": s,
                                "end_ts": e,
                                "video_duration_sec": _dur,
                            }
                        ),
                    )

                    self._emit(
                        {
                            "event": "chunk_complete",
                            "chunk_index": work_order["chunk_index"],
                            "end_ts": work_order["end_ts"],
                            "video_duration_sec": duration_sec,
                        }
                    )
                finally:
                    proxy_path.unlink(missing_ok=True)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        return {}

    def _request(self, method: str, path: str, **kwargs: object):
        """Raw request for endpoints that use 204/409; returns response without exiting."""
        return self._client.raw(method, path, **kwargs)

    def _emit(self, event: dict) -> None:
        if self._progress_callback:
            try:
                self._progress_callback(event)
            except Exception:
                # never let progress crash the worker
                pass

    def _transcode_chunk_proxy(
        self,
        source: Path,
        dest: Path,
        start_sec: float,
        duration_sec: float,
    ) -> bool:
        """Transcode a chunk window from source to a 720p H.264 proxy."""
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            str(start_sec),
            "-i",
            str(source),
            "-t",
            str(duration_sec),
            "-vf",
            "scale=-2:720",
            "-c:v",
            "libx264",
            "-b:v",
            "3M",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(dest),
        ]
        logger.debug("Running: %s", " ".join(str(a) for a in cmd))
        result = subprocess.run(cmd, capture_output=True)
        logger.debug("Exited %d: %s", result.returncode, cmd[0])
        if result.returncode != 0:
            logger.warning(
                "Chunk transcode failed (exit %d): %s",
                result.returncode,
                result.stderr.decode(errors="replace"),
            )
        return result.returncode == 0

    def _process_chunk(
        self,
        source: Path,
        proxy_path: Path,
        chunk_offset: float,
        work_order: dict,
        tmpdir: Path,
        storage: LocalStorage,
        tenant_id: str,
        library_id: str,
        asset_id: str,
        frame_callback: Callable[[float, float, float], None] | None = None,
    ) -> None:
        """Run scene detection on chunk, extract rep frames, complete or fail chunk."""
        chunk_id = work_order["chunk_id"]
        worker_id = work_order["worker_id"]
        scan_start = 0.0
        scan_end = work_order["end_ts"] - chunk_offset

        try:
            collected: list[RawFrame] = []
            scanner = VideoScanner(proxy_path)
            try:
                for raw in scanner.scan(start_ts=scan_start, end_ts=scan_end):
                    collected.append(raw)
                    if frame_callback:
                        frame_callback(
                            raw.pts + chunk_offset,
                            work_order["start_ts"],
                            work_order["end_ts"],
                        )
            except Exception as scan_err:
                logger.debug(
                    "VideoIndexWorker scanner exception while scanning chunk %s: %r",
                    chunk_id,
                    scan_err,
                )
                raise
            logger.debug(
                "VideoIndexWorker after scan: collected=%d for chunk=%s",
                len(collected),
                chunk_id,
            )

            if not collected:
                logger.info(
                    "No keyframes found in chunk %s (%.1f–%.1f); falling back to interval extraction",
                    work_order["chunk_id"],
                    work_order["start_ts"],
                    work_order["end_ts"],
                )
                t = 0.0
                while t < scan_end:
                    frame_path = tmpdir / f"{asset_id}_fallback_{int((t + chunk_offset) * 1000)}.jpg"
                    if extract_video_frame(proxy_path, frame_path, timestamp=t):
                        img = Image.open(frame_path).convert("RGB")
                        w, h = img.size
                        raw_bytes = img.tobytes()
                        collected.append(
                            RawFrame(bytes=raw_bytes, pts=t, width=w, height=h)
                        )
                        frame_path.unlink(missing_ok=True)
                        if frame_callback:
                            frame_callback(
                                t + chunk_offset,
                                work_order["start_ts"],
                                work_order["end_ts"],
                            )
                    t += DEBOUNCE_SEC

            if not collected:
                logger.warning(
                    "No frames available for chunk %s after fallback; completing with empty scenes",
                    chunk_id,
                )

            scene_start_ts = work_order.get("scene_start_ts")
            if scene_start_ts is not None:
                scene_start_ts = max(0.0, scene_start_ts - chunk_offset)
            segmenter = SceneSegmenter(
                frames=collected,
                anchor_phash=work_order.get("anchor_phash"),
                scene_start_ts=scene_start_ts,
            )
            scenes_raw = segmenter.segment()

            start_boundary_ms = int((work_order["start_ts"] - chunk_offset) * 1000)
            filtered = [s for s in scenes_raw if s.end_ms >= start_boundary_ms]

            scene_dicts = []
            for i, scene in enumerate(filtered):
                chunk_offset_ms = int(round(chunk_offset * 1000))
                abs_rep_ms = scene.rep_frame_ms + chunk_offset_ms
                abs_start_ms = scene.start_ms + chunk_offset_ms
                abs_end_ms = scene.end_ms + chunk_offset_ms
                rep_path = tmpdir / f"{asset_id}_{abs_rep_ms}.jpg"
                extract_video_frame(source, rep_path, timestamp=abs_rep_ms / 1000.0)
                key = storage.scene_rep_key(
                    tenant_id=tenant_id,
                    library_id=library_id,
                    asset_id=asset_id,
                    rep_frame_ms=abs_rep_ms,
                )
                dest = storage.abs_path(key)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(rep_path, dest)
                rep_path.unlink(missing_ok=True)
                scene_dicts.append({
                    "scene_index": i,
                    "start_ms": abs_start_ms,
                    "end_ms": abs_end_ms,
                    "rep_frame_ms": abs_rep_ms,
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
            if next_scene_start_ms is not None:
                next_scene_start_ms = next_scene_start_ms + int(round(chunk_offset * 1000))

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
