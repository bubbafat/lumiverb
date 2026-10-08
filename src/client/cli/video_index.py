"""Video scene detection and enrichment orchestration for CLI ingest and repair.

Scene detection: runs VideoScanner + SceneSegmenter on each video's
analysis proxy, submits scene boundaries to the server via the chunk API.

Scene enrichment: extracts rep frame JPEGs at each scene's timestamp,
uploads as artifacts, runs vision AI, and syncs to search. Scenes go to
the vision machines as many at once as they take together, across videos.

Used by `lumiverb ingest` (stages 2+3) and `lumiverb repair`.
"""

from __future__ import annotations

import io
import logging
import tempfile
import time
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.progress import Progress

from src.client.cli.client import LumiverbClient
from src.client.video.clip_extractor import extract_video_frame_detailed
from src.client.video.scene_segmenter import SceneSegmenter
from src.client.video.video_scanner import SyncError, VideoScanner

# Where to read a video from: its analysis proxy, or None when there isn't one.
SourceFor = Callable[[dict], Path | None]

logger = logging.getLogger(__name__)


def index_video_scenes(
    *,
    client: LumiverbClient,
    source_path: Path,
    asset_id: str,
    duration_sec: float,
    rel_path: str,
    lineage: dict | None = None,
) -> dict:
    """Run scene detection on a single video and submit results to server.
    lineage: how the scenes are found, recorded when the last chunk completes.

    Returns {"scenes": N, "chunks": N, "elapsed": float}.
    """

    t0 = time.perf_counter()
    total_scenes = 0
    total_chunks = 0
    chunk_errors: list[str] = []

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
                    "lineage": lineage,
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
            chunk_errors.append(str(e))
            client.post(
                f"/v1/video/chunks/{chunk_id}/fail",
                json={"worker_id": worker_id, "error_message": str(e)},
            )
        except Exception as e:
            logger.exception("video-index: %s chunk %d — failed: %s", rel_path, work["chunk_index"], e)
            chunk_errors.append(str(e) or type(e).__name__)
            client.post(
                f"/v1/video/chunks/{chunk_id}/fail",
                json={"worker_id": worker_id, "error_message": str(e)},
            )

    if chunk_errors:
        # The video isn't indexed until every chunk is: it's tried again once its turn comes.
        raise RuntimeError(f"{len(chunk_errors)} of {total_chunks + len(chunk_errors)} chunks failed: {chunk_errors[0]}")
    elapsed = time.perf_counter() - t0
    return {"scenes": total_scenes, "chunks": total_chunks, "elapsed": elapsed}


def run_video_index(
    *,
    client: LumiverbClient,
    source_for: SourceFor,
    videos: Iterable[dict],
    console: Console,
    progress: Progress,
    task_id: object,
    lineage_for: "Callable[[dict], dict] | None" = None,
    on_fail: "Callable[[str, object], None] | None" = None,
) -> tuple[int, int]:
    """Run scene detection on a batch of videos, updating progress.
    on_fail(asset_id, error) hears of each video it couldn't do.

    Each video dict must have: asset_id, rel_path, duration_sec.
    Videos are processed sequentially (FFmpeg is CPU/IO heavy).
    Returns (ok, failed).
    """
    ok = 0
    fail = 0

    for video in videos:
        asset_id = video["asset_id"]
        rel_path = video["rel_path"]
        duration_sec = video["duration_sec"]
        source_path = source_for(video)

        if source_path is None or not source_path.is_file():
            logger.warning("video-index: %s — no analysis proxy, skipping", rel_path)
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
                lineage=lineage_for(video) if lineage_for else None,
            )
            ok += 1
            logger.info(
                "video-index: %s — %d scenes in %d chunks (%.1fs)",
                rel_path, result["scenes"], result["chunks"], result["elapsed"],
            )
        except Exception as e:
            logger.exception("video-index: %s — failed: %s", rel_path, e)
            if on_fail:
                on_fail(asset_id, e)
            fail += 1
            progress.console.print(f"[red]video-index \u2717[/red] {rel_path}: {e}")

        progress.advance(task_id, 1)
        progress.update(task_id, ok=ok, fail=fail)

    return ok, fail


# ---------------------------------------------------------------------------
# Scene enrichment: rep frame extraction + vision AI + search sync
# ---------------------------------------------------------------------------


class _NoFrameError(Exception):
    """The scene's representative frame couldn't be read from the proxy."""


def enrich_scene(
    *,
    client: LumiverbClient,
    source_path: Path,
    asset_id: str,
    rel_path: str,
    scene: dict,
    vision_provider: object | None,
    vision_model_id: str | None,
    lineage: dict | None = None,
) -> None:
    """Extract one scene's rep frame, run vision AI, and sync it to search.
    lineage: how the description is made, recorded with it.

    Raises when it can't be done. The vision endpoint failing raises
    CaptionError with endpoint_fault: it isn't the scene's fault.
    """
    scene_id = scene["scene_id"]
    rep_frame_ms = scene["rep_frame_ms"]

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        # 1. Extract rep frame JPEG
        attempt = extract_video_frame_detailed(
            source_path, tmp_path,
            timestamp=rep_frame_ms / 1000.0,
        )
        if not attempt.ok or not tmp_path.exists() or tmp_path.stat().st_size == 0:
            raise _NoFrameError(f"no frame at {rep_frame_ms} ms")

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
                        "lineage": lineage,
                    },
                )

                # 5. Sync scene to Quickwit
                client.post(
                    f"/v1/video/scenes/{scene_id}/sync",
                    json={"asset_id": asset_id},
                )

        logger.info(
            "scene-enrich: %s scene %s — rep frame at %dms%s",
            rel_path, scene_id, rep_frame_ms,
            " + vision" if vision_provider else "",
        )
    finally:
        tmp_path.unlink(missing_ok=True)


@dataclass(eq=False)
class _VideoScenes:
    """A video's scenes under way: it's reported once every one handed out is back."""
    video: dict
    t0: float
    skipped: int = 0
    pending: int = 0
    enriched: int = 0
    errors: list[str] = field(default_factory=list)
    # The vision endpoint failed (or stopped the run) before its scenes were done: not its fault.
    fault: Exception | None = None
    handed_out: bool = False


def run_video_enrich(
    *,
    client: LumiverbClient,
    source_for: SourceFor,
    videos: Iterable[dict],
    vision_provider: object | None,
    vision_model_id: str | None,
    console: Console,
    progress: Progress,
    task_id: object,
    lineage_for: "Callable[[dict], dict] | None" = None,
    on_fail: "Callable[[str, object], None] | None" = None,
    concurrency: int = 1,
) -> tuple[int, int]:
    """Run scene enrichment on a batch of videos, updating progress per video.
    on_fail(asset_id, error) hears of each video it couldn't do; a scene's
    failure is reported for its video once all its scenes are back.

    Scenes go out `concurrency` at once, across videos, so short videos
    don't leave the vision machines idle; results are gathered here. The
    vision endpoint failing (CaptionError with endpoint_fault: no machine
    left) stops handing out scenes, and each video it cut short hears of
    it with that error, which is no video's fault.

    Each video dict must have: asset_id, rel_path.
    Returns (ok, failed).
    """
    from src.client.workers.captions.base import CaptionError

    concurrency = max(1, concurrency)
    ok = 0
    fail = 0
    fault: CaptionError | None = None

    def _report(v: _VideoScenes) -> None:
        nonlocal ok, fail
        asset_id, rel_path = v.video["asset_id"], v.video["rel_path"]
        logger.info(
            "scene-enrich: %s — %d enriched, %d skipped, %d failed (%.1fs)",
            rel_path, v.enriched, v.skipped, len(v.errors), time.perf_counter() - v.t0,
        )
        if v.fault is not None:
            logger.warning("scene-enrich: %s — stopped: %s", rel_path, v.fault)
            if on_fail:
                on_fail(asset_id, v.fault)
            fail += 1
            progress.console.print(f"[red]scene-enrich \u2717[/red] {rel_path}: {v.fault}")
        elif v.errors:
            # Its undescribed scenes are handed out again once its turn comes.
            if on_fail:
                on_fail(asset_id, f"{len(v.errors)} of {v.enriched + len(v.errors)} scenes failed: {v.errors[0]}")
            fail += 1
        else:
            ok += 1
        progress.advance(task_id, 1)
        progress.update(task_id, ok=ok, fail=fail)

    def _enrich(v: _VideoScenes, source_path: Path, scene: dict, lineage: dict | None) -> tuple[_VideoScenes, object]:
        """enrich_scene, on a worker thread: (v, None) when done, else (v, why)."""
        rel_path, scene_id = v.video["rel_path"], scene["scene_id"]
        try:
            enrich_scene(client=client, source_path=source_path, asset_id=v.video["asset_id"], rel_path=rel_path,
                         scene=scene, vision_provider=vision_provider, vision_model_id=vision_model_id,
                         lineage=lineage)
            return v, None
        except CaptionError as e:
            if e.endpoint_fault:
                return v, e
            logger.warning("scene-enrich: %s scene %s — the model couldn't describe it: %s", rel_path, scene_id, e)
            return v, str(e)
        except _NoFrameError as e:
            logger.warning("scene-enrich: %s scene %s — rep frame extraction failed: %s", rel_path, scene_id, e)
            return v, str(e)
        except Exception as e:
            logger.exception("scene-enrich: %s scene %s — failed: %s", rel_path, scene_id, e)
            return v, str(e) or type(e).__name__

    def _gather(fut: Future) -> None:
        nonlocal fault
        v, error = fut.result()
        v.pending -= 1
        if error is None:
            v.enriched += 1
        elif isinstance(error, CaptionError):
            v.fault = error
            fault = fault or error
        else:
            v.errors.append(error)
        if v.handed_out and v.pending == 0:
            _report(v)

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="scene-vision") as pool:
        inflight: set[Future] = set()
        for video in videos:
            if fault is not None:
                break
            asset_id = video["asset_id"]
            rel_path = video["rel_path"]
            source_path = source_for(video)

            if source_path is None or not source_path.is_file():
                logger.warning("scene-enrich: %s — no analysis proxy, skipping", rel_path)
                fail += 1
                progress.advance(task_id, 1)
                progress.update(task_id, ok=ok, fail=fail)
                continue

            try:
                scenes = client.get(f"/v1/video/{asset_id}/scenes").json().get("scenes", [])
                lineage = lineage_for(video) if lineage_for else None
            except Exception as e:
                logger.exception("scene-enrich: %s — failed: %s", rel_path, e)
                if on_fail:
                    on_fail(asset_id, e)
                fail += 1
                progress.console.print(f"[red]scene-enrich \u2717[/red] {rel_path}: {e}")
                progress.advance(task_id, 1)
                progress.update(task_id, ok=ok, fail=fail)
                continue

            v = _VideoScenes(video=video, t0=time.perf_counter())
            # Only an approved upgrade hands out a video with every scene
            # described: describe them all again. Otherwise pick up where it
            # left off, skipping scenes already described.
            again = bool(scenes) and all(scene.get("description") for scene in scenes)
            for scene in scenes:
                if scene.get("description") and not again:
                    v.skipped += 1
                    continue
                if fault is not None:
                    v.fault = v.fault or fault
                    break
                v.pending += 1
                inflight.add(pool.submit(_enrich, v, source_path, scene, lineage))
                if len(inflight) >= concurrency:
                    done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                    for fut in done:
                        _gather(fut)
            v.handed_out = True
            if v.pending == 0:
                _report(v)
        for fut in as_completed(inflight):
            _gather(fut)

    return ok, fail
