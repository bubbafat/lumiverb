"""Video scene detection and enrichment orchestration for CLI ingest and repair.

Scene detection: runs VideoScanner + SceneSegmenter on local video files,
submits scene boundaries to the server via the chunk API.

Scene enrichment: extracts rep frame JPEGs at each scene's timestamp,
uploads as artifacts, runs vision AI, and syncs to search.

Used by `lumiverb ingest` (stages 2+3) and `lumiverb repair`.
"""

from __future__ import annotations

import io
import logging
import tempfile
import time
from pathlib import Path

from rich.console import Console
from rich.progress import Progress

from src.client.cli.client import LumiverbClient
from src.client.video.clip_extractor import extract_video_frame_detailed
from src.client.video.scene_segmenter import SceneSegmenter
from src.client.video.video_scanner import SyncError, VideoScanner

logger = logging.getLogger(__name__)


def index_video_scenes(
    *,
    client: LumiverbClient,
    source_path: Path,
    asset_id: str,
    duration_sec: float,
    rel_path: str,
) -> dict:
    """Run scene detection on a single video and submit results to server.

    Returns {"scenes": N, "chunks": N, "elapsed": float}.
    """

    t0 = time.perf_counter()
    total_scenes = 0
    total_chunks = 0

    # 1. Init chunks (idempotent — safe for retry/repair)
    resp = client.post(
        f"/v1/video/{asset_id}/chunks",
        json={"duration_sec": duration_sec},
    )
    init = resp.json()
    logger.info(
        "video-index: %s — %d chunks (%s)",
        rel_path,
        init["chunk_count"],
        "already initialized" if init["already_initialized"] else "created",
    )

    # 2. Claim and process chunks
    scanner = VideoScanner(source_path)

    while True:
        claim_resp = client.raw("GET", f"/v1/video/{asset_id}/chunks/next")
        if claim_resp.status_code == 204:
            break  # all chunks done
        claim_resp.raise_for_status()
        work = claim_resp.json()

        chunk_id = work["chunk_id"]
        worker_id = work["worker_id"]
        start_ts = work["start_ts"]
        end_ts = work["end_ts"]
        overlap = work.get("overlap_sec", 2.0)
        anchor_phash = work.get("anchor_phash")
        scene_start_ts = work.get("scene_start_ts")

        try:
            # Scan with overlap for anchor continuity
            scan_start = max(0.0, start_ts - overlap)
            frames = scanner.scan(scan_start, end_ts)

            segmenter = SceneSegmenter(
                frames,
                anchor_phash=anchor_phash,
                scene_start_ts=scene_start_ts,
            )
            scenes = segmenter.segment()

            # Build scene results for server
            scene_results = []
            for i, scene in enumerate(scenes):
                scene_results.append({
                    "scene_index": total_scenes + i,
                    "start_ms": scene.start_ms,
                    "end_ms": scene.end_ms,
                    "rep_frame_ms": scene.rep_frame_ms,
                    "sharpness_score": scene.sharpness_score,
                    "keep_reason": scene.keep_reason,
                    "phash": scene.phash,
                })

            # Submit completed chunk
            client.post(
                f"/v1/video/chunks/{chunk_id}/complete",
                json={
                    "worker_id": worker_id,
                    "scenes": scene_results,
                    "next_anchor_phash": segmenter.next_anchor_phash,
                    "next_scene_start_ms": segmenter.next_scene_start_ms,
                },
            )

            total_scenes += len(scenes)
            total_chunks += 1
            logger.info(
                "video-index: %s chunk %d — %d scenes",
                rel_path, work["chunk_index"], len(scenes),
            )

        except SyncError as e:
            logger.warning("video-index: %s chunk %d — FFmpeg sync error: %s", rel_path, work["chunk_index"], e)
            client.post(
                f"/v1/video/chunks/{chunk_id}/fail",
                json={"worker_id": worker_id, "error_message": str(e)},
            )
        except Exception as e:
            logger.exception("video-index: %s chunk %d — failed: %s", rel_path, work["chunk_index"], e)
            client.post(
                f"/v1/video/chunks/{chunk_id}/fail",
                json={"worker_id": worker_id, "error_message": str(e)},
            )

    elapsed = time.perf_counter() - t0
    return {"scenes": total_scenes, "chunks": total_chunks, "elapsed": elapsed}


def run_video_index(
    *,
    client: LumiverbClient,
    root_path: Path,
    videos: list[dict],
    console: Console,
    progress: Progress,
    task_id: object,
) -> None:
    """Run scene detection on a batch of videos, updating progress.

    Each video dict must have: asset_id, rel_path, duration_sec.
    Videos are processed sequentially (FFmpeg is CPU/IO heavy).
    """
    ok = 0
    fail = 0

    for video in videos:
        asset_id = video["asset_id"]
        rel_path = video["rel_path"]
        duration_sec = video["duration_sec"]
        source_path = (root_path / rel_path).resolve()

        if not source_path.is_file():
            logger.warning("video-index: %s — source file not found, skipping", rel_path)
            fail += 1
            progress.advance(task_id, 1)
            progress.update(task_id, ok=ok, fail=fail)
            continue

        try:
            result = index_video_scenes(
                client=client,
                source_path=source_path,
                asset_id=asset_id,
                duration_sec=duration_sec,
                rel_path=rel_path,
            )
            ok += 1
            logger.info(
                "video-index: %s — %d scenes in %d chunks (%.1fs)",
                rel_path, result["scenes"], result["chunks"], result["elapsed"],
            )
        except Exception as e:
            logger.exception("video-index: %s — failed: %s", rel_path, e)
            fail += 1
            progress.console.print(f"[red]video-index \u2717[/red] {rel_path}: {e}")

        progress.advance(task_id, 1)
        progress.update(task_id, ok=ok, fail=fail)


# ---------------------------------------------------------------------------
# Scene enrichment: rep frame extraction + vision AI + search sync
# ---------------------------------------------------------------------------


def enrich_video_scenes(
    *,
    client: LumiverbClient,
    source_path: Path,
    asset_id: str,
    rel_path: str,
    vision_provider: object | None,
    vision_model_id: str | None,
) -> dict:
    """Extract rep frames, run vision AI, and sync scenes to search.

    Returns {"enriched": N, "skipped": N, "failed": N, "elapsed": float}.
    """
    t0 = time.perf_counter()
    enriched = 0
    skipped = 0
    failed = 0

    # List all scenes for this asset
    resp = client.get(f"/v1/video/{asset_id}/scenes")
    scenes = resp.json().get("scenes", [])

    for scene in scenes:
        scene_id = scene["scene_id"]
        rep_frame_ms = scene["rep_frame_ms"]

        # Skip scenes that already have vision descriptions
        if scene.get("description"):
            skipped += 1
            continue

        tmp_path = None
        try:
            # 1. Extract rep frame JPEG
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = Path(tmp.name)

            attempt = extract_video_frame_detailed(
                source_path, tmp_path,
                timestamp=rep_frame_ms / 1000.0,
            )
            if not attempt.ok or not tmp_path.exists() or tmp_path.stat().st_size == 0:
                logger.warning(
                    "scene-enrich: %s scene %s — rep frame extraction failed at %dms",
                    rel_path, scene_id, rep_frame_ms,
                )
                failed += 1
                continue

            # 2. Upload rep frame artifact
            with open(tmp_path, "rb") as f:
                client.post(
                    f"/v1/assets/{asset_id}/artifacts/scene_rep",
                    files={"file": ("rep.jpg", f, "image/jpeg")},
                    data={"rep_frame_ms": str(rep_frame_ms)},
                )

            # 3. Run vision AI (if provider available)
            if vision_provider is not None and vision_model_id:
                result = vision_provider.describe(tmp_path)
                if result:
                    description = (result.get("description") or "").strip()
                    tags = [
                        t.strip()
                        for t in (result.get("tags") or [])
                        if isinstance(t, str) and t.strip()
                    ]

                    # 4. PATCH scene with vision results
                    client.patch(
                        f"/v1/video/scenes/{scene_id}",
                        json={
                            "model_id": vision_model_id,
                            "model_version": "1",
                            "description": description,
                            "tags": tags,
                        },
                    )

                    # 5. Sync scene to Quickwit
                    client.post(
                        f"/v1/video/scenes/{scene_id}/sync",
                        json={"asset_id": asset_id},
                    )

            enriched += 1
            logger.info(
                "scene-enrich: %s scene %s — rep frame at %dms%s",
                rel_path, scene_id, rep_frame_ms,
                " + vision" if vision_provider else "",
            )

        except Exception as e:
            logger.exception(
                "scene-enrich: %s scene %s — failed: %s",
                rel_path, scene_id, e,
            )
            failed += 1
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    elapsed = time.perf_counter() - t0
    return {"enriched": enriched, "skipped": skipped, "failed": failed, "elapsed": elapsed}


def run_video_enrich(
    *,
    client: LumiverbClient,
    root_path: Path,
    videos: list[dict],
    vision_provider: object | None,
    vision_model_id: str | None,
    console: Console,
    progress: Progress,
    task_id: object,
) -> None:
    """Run scene enrichment on a batch of videos, updating progress.

    Each video dict must have: asset_id, rel_path.
    Videos are processed sequentially (vision API is the bottleneck).
    """
    ok = 0
    fail = 0

    for video in videos:
        asset_id = video["asset_id"]
        rel_path = video["rel_path"]
        source_path = (root_path / rel_path).resolve()

        if not source_path.is_file():
            logger.warning("scene-enrich: %s — source file not found, skipping", rel_path)
            fail += 1
            progress.advance(task_id, 1)
            progress.update(task_id, ok=ok, fail=fail)
            continue

        try:
            result = enrich_video_scenes(
                client=client,
                source_path=source_path,
                asset_id=asset_id,
                rel_path=rel_path,
                vision_provider=vision_provider,
                vision_model_id=vision_model_id,
            )
            ok += 1
            logger.info(
                "scene-enrich: %s — %d enriched, %d skipped, %d failed (%.1fs)",
                rel_path, result["enriched"], result["skipped"],
                result["failed"], result["elapsed"],
            )
        except Exception as e:
            logger.exception("scene-enrich: %s — failed: %s", rel_path, e)
            fail += 1
            progress.console.print(f"[red]scene-enrich \u2717[/red] {rel_path}: {e}")

        progress.advance(task_id, 1)
        progress.update(task_id, ok=ok, fail=fail)
