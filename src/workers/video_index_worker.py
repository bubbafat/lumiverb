"""Video index worker.

Claims `video-index` jobs, transcodes the source video to a 720p proxy, and
processes the asset in 30-second chunks using the video chunk API:

- POST /v1/video/{asset_id}/chunks          (init chunks)
- GET  /v1/video/{asset_id}/chunks/next    (claim next chunk)
- POST /v1/video/chunks/{chunk_id}/complete
- POST /v1/video/chunks/{chunk_id}/fail

Scene segmentation is performed locally using VideoScanner + SceneSegmenter,
and high-resolution representative frames are extracted from the full source
via FFmpeg into LocalStorage using scene_rep_key().
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.core.config import get_settings
from src.storage.local import LocalStorage
from src.video.clip_extractor import extract_video_frame, transcode_to_720p_h264
from src.video.video_scanner import VideoScanner
from src.video.scene_segmenter import SceneSegmenter
from src.workers.base import BaseWorker

logger = logging.getLogger(__name__)


class VideoIndexWorker(BaseWorker):
    job_type = "video-index"

    def __init__(
        self,
        client: object,
        storage: LocalStorage | None = None,
        tenant_id: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(client=client, **kwargs)
        self._storage = storage
        self._tenant_id = tenant_id

    # ------------------------------------------------------------------
    # Public API (BaseWorker hook)
    # ------------------------------------------------------------------

    def process(self, job: dict) -> dict | None:
        """
        Process a single video-index job for an asset.

        After finishing all chunks, BaseWorker.run() will call
        complete_job(job_id, result or {}), so we return {}.
        """
        asset_id = job["asset_id"]
        rel_path = job["rel_path"]
        root_path = job["root_path"]
        library_id = job["library_id"]

        source_path = Path(root_path) / rel_path
        if not source_path.exists():
            raise FileNotFoundError(f"Source video not found: {source_path}")

        duration_sec = job.get("duration_sec")
        if duration_sec is None or duration_sec <= 0:
            raise ValueError(f"Missing or invalid duration_sec for asset {asset_id}")

        settings = get_settings()
        # Stable per-asset proxy location under data_dir.
        proxy_dir = Path(settings.data_dir) / "tmp" / "video_index"
        proxy_dir.mkdir(parents=True, exist_ok=True)
        proxy_path = proxy_dir / f"{asset_id}_proxy_720p.mp4"

        logger.info("Transcoding asset_id=%s to 720p proxy at %s", asset_id, proxy_path)
        ok = transcode_to_720p_h264(source_path, proxy_path)
        if not ok or not proxy_path.exists():
            raise RuntimeError(f"Failed to transcode video to proxy for asset {asset_id}")

        # Initialize chunks (idempotent).
        self._init_chunks(asset_id=asset_id, duration_sec=float(duration_sec))

        # Process chunks until /next returns 204.
        while True:
            work_order = self._claim_next_chunk(asset_id)
            if work_order is None:
                break
            self._process_chunk(
                source=source_path,
                proxy_path=proxy_path,
                work_order=work_order,
                asset_id=asset_id,
                library_id=library_id,
            )

        return {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _init_chunks(self, asset_id: str, duration_sec: float) -> None:
        """POST /v1/video/{asset_id}/chunks to pre-generate chunk rows."""
        # Use low-level request so workers can inspect status codes if needed.
        client = self._client
        resp = getattr(client, "request")(  # type: ignore[call-arg]
            "POST",
            f"/v1/video/{asset_id}/chunks",
            json={"duration_sec": duration_sec},
        )
        if resp.status_code // 100 != 2:
            # Surface details for debugging; jobs API wrapper will turn this into a failed job.
            raise RuntimeError(
                f"Failed to init video chunks for asset {asset_id}: "
                f"HTTP {resp.status_code} {resp.text}"
            )

    def _claim_next_chunk(self, asset_id: str) -> dict[str, Any] | None:
        """GET /v1/video/{asset_id}/chunks/next; return work_order dict or None on 204."""
        client = self._client
        resp = getattr(client, "request")(  # type: ignore[call-arg]
            "GET",
            f"/v1/video/{asset_id}/chunks/next",
        )
        if resp.status_code == 204:
            return None
        if resp.status_code // 100 != 2:
            raise RuntimeError(
                f"Failed to claim next video chunk for asset {asset_id}: "
                f"HTTP {resp.status_code} {resp.text}"
            )
        data = resp.json()
        assert isinstance(data, dict)
        return data

    def _ensure_storage_and_tenant(self) -> tuple[LocalStorage, str]:
        """
        Ensure LocalStorage instance and tenant_id are available.

        tenant_id is resolved via GET /v1/tenant/context so workers do not
        depend on client-specific attributes.
        """
        if self._storage is None:
            self._storage = LocalStorage()
        if self._tenant_id is None:
            client = self._client
            resp = getattr(client, "request")(  # type: ignore[call-arg]
                "GET",
                "/v1/tenant/context",
            )
            if resp.status_code // 100 != 2:
                raise RuntimeError(
                    f"Failed to resolve tenant context: HTTP {resp.status_code} {resp.text}"
                )
            body = resp.json()
            self._tenant_id = str(body.get("tenant_id"))
            if not self._tenant_id:
                raise RuntimeError("tenant_id missing from /v1/tenant/context response")
        return self._storage, self._tenant_id

    def _process_chunk(
        self,
        source: Path,
        proxy_path: Path,
        work_order: dict[str, Any],
        *,
        asset_id: str,
        library_id: str,
    ) -> None:
        """
        Process a single chunk using VideoScanner + SceneSegmenter and complete it.

        Error handling contract:
        - On any exception while processing this chunk, POST /v1/video/chunks/{chunk_id}/fail
          with {"worker_id", "error_message"} and then re-raise so BaseWorker.run()
          causes the job to be marked failed.
        """
        chunk_id = work_order["chunk_id"]
        worker_id = work_order["worker_id"]

        scan_start = max(0.0, float(work_order["start_ts"]) - float(work_order["overlap_sec"]))
        scan_end = float(work_order["end_ts"])

        storage, tenant_id = self._ensure_storage_and_tenant()

        client = self._client

        try:
            # Scene detection on proxy
            scanner = VideoScanner(proxy_path)
            raw_frames = scanner.scan(start_ts=scan_start, end_ts=scan_end)
            segmenter = SceneSegmenter(
                frames=raw_frames,
                anchor_phash=work_order.get("anchor_phash"),
                scene_start_ts=work_order.get("scene_start_ts"),
            )
            scenes_raw = segmenter.segment()

            # Discard overlap scenes that end entirely before this chunk's logical start.
            start_boundary_ms = int(float(work_order["start_ts"]) * 1000)
            filtered_scenes: list[Any] = []
            for scene in scenes_raw:
                end_ms = int(getattr(scene, "end_ms"))
                if end_ms <= start_boundary_ms:
                    continue
                filtered_scenes.append(scene)

            scenes_payload: list[dict[str, Any]] = []
            for idx, scene in enumerate(filtered_scenes):
                rep_frame_ms = int(getattr(scene, "rep_frame_ms"))
                # Compute storage key and extract high-res rep frame directly into that path.
                scene_rep_key = storage.scene_rep_key(
                    tenant_id=tenant_id,
                    library_id=library_id,
                    asset_id=asset_id,
                    rep_frame_ms=rep_frame_ms,
                )
                dest_path = storage.abs_path(scene_rep_key)
                ok = extract_video_frame(
                    source=source,
                    dest=dest_path,
                    timestamp=rep_frame_ms / 1000.0,
                )
                if not ok or not dest_path.exists():
                    raise RuntimeError(
                        f"Failed to extract rep frame for scene at {rep_frame_ms}ms "
                        f"(asset_id={asset_id}, chunk_id={chunk_id})"
                    )

                sharpness_score = getattr(scene, "sharpness_score", None)
                keep_reason = getattr(scene, "keep_reason", None)
                phash = getattr(scene, "phash", None)

                scenes_payload.append(
                    {
                        "scene_index": idx,
                        "start_ms": int(getattr(scene, "start_ms")),
                        "end_ms": int(getattr(scene, "end_ms")),
                        "rep_frame_ms": rep_frame_ms,
                        "proxy_key": None,
                        # Rep frame stored as thumbnail_key for now.
                        "thumbnail_key": scene_rep_key,
                        "description": None,
                        "tags": None,
                        "sharpness_score": float(sharpness_score)
                        if sharpness_score is not None
                        else None,
                        "keep_reason": str(keep_reason) if keep_reason else None,
                        "phash": phash,
                    }
                )

            # Anchor propagation for next chunk: prefer explicit attributes if present.
            next_anchor_phash = getattr(segmenter, "next_anchor_phash", None)
            next_scene_start_ms = getattr(segmenter, "next_scene_start_ms", None)

            # Fallback: use last scene's phash as anchor if segmenter does not expose it.
            if next_anchor_phash is None and scenes_raw:
                last_scene = scenes_raw[-1]
                next_anchor_phash = getattr(last_scene, "phash", None)

            payload = {
                "worker_id": worker_id,
                "scenes": scenes_payload,
                "next_anchor_phash": next_anchor_phash,
                "next_scene_start_ms": next_scene_start_ms,
            }

            resp = getattr(client, "request")(  # type: ignore[call-arg]
                "POST",
                f"/v1/video/chunks/{chunk_id}/complete",
                json=payload,
            )
            if resp.status_code == 409:
                # Lease expired or chunk no longer owned — log and continue; server will reclaim.
                logger.warning(
                    "Chunk completion conflict for chunk_id=%s worker_id=%s: HTTP 409",
                    chunk_id,
                    worker_id,
                )
                return
            if resp.status_code // 100 != 2:
                raise RuntimeError(
                    f"Failed to complete chunk {chunk_id}: HTTP {resp.status_code} {resp.text}"
                )
        except Exception as e:
            # Best-effort fail notification; ignore 409 (lost lease) here as well.
            try:
                resp = getattr(client, "request")(  # type: ignore[call-arg]
                    "POST",
                    f"/v1/video/chunks/{chunk_id}/fail",
                    json={"worker_id": worker_id, "error_message": str(e)},
                )
                if resp.status_code == 409:
                    logger.warning(
                        "Chunk fail conflict for chunk_id=%s worker_id=%s: HTTP 409",
                        chunk_id,
                        worker_id,
                    )
                elif resp.status_code // 100 != 2:
                    logger.error(
                        "Failed to report chunk failure for chunk_id=%s worker_id=%s: "
                        "HTTP %s %s",
                        chunk_id,
                        worker_id,
                        resp.status_code,
                        resp.text,
                    )
            except Exception as report_err:  # pragma: no cover - defensive
                logger.error(
                    "Error while reporting failure for chunk_id=%s: %s",
                    chunk_id,
                    report_err,
                )
            # Re-raise original error so BaseWorker marks the job failed.
            raise

