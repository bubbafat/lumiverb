"""Unified repair command: detect and fix missing pipeline outputs."""

from __future__ import annotations

import gc
import io
import json
import logging
import threading
import multiprocessing as mp
import os
from collections.abc import Callable, Collection, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, as_completed, wait
from typing import TYPE_CHECKING, Literal

from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, MofNCompleteColumn, TimeRemainingColumn, SpinnerColumn
from rich.table import Table

from src.client.cli.client import LumiverbClient
from src.client.video.analysis_proxy import (
    AnalysisProxySettings,
    RenderError,
    gpu_decoder,
    render_analysis_proxy,
    render_timeout,
)
from src.client.video.audio import audio_tracks, speech_wav_command
from src.client.video.probe import probe_video
from src.client.workers.faces.insightface_provider import InsightFaceProvider
from src.shared.io_utils import resolve_source_path
from src.shared.producers import PRODUCERS

if TYPE_CHECKING:
    from pathlib import Path

    from src.client.cli.producer_settings import ProducerSettings
    from src.client.proxy.analysis_cache import AnalysisProxyCache
    from src.client.workers.transcripts.base import Transcriber

logger = logging.getLogger(__name__)

# The proxy cache's long edge: the images CLIP, faces and vision AI are given.
PROXY_CACHE_EDGE: int = PRODUCERS["clip"].defaults["input_edge"]


def _silence_subprocess_stdout() -> None:
    """Redirect stdout to /dev/null in subprocess to suppress InsightFace/ONNX print noise."""
    import os
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.close(devnull)


def _drain(inflight: set[Future]) -> set[Future]:
    """Wait for at least one future to complete and return the remaining set."""
    done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
    for fut in done:
        fut.result()  # re-raise if failed
    return inflight


# Faces run in batches of this many when a run may be told to stop, so it
# can stop between them (each batch starts its own detection processes).
FACE_CHUNK = 500


def _until[T](stop: Callable[[], bool], items: Iterable[T], taken: Callable[[T], None] | None = None) -> Iterator[T]:
    """items, one at a time, until stop() says to stop. taken(item) hears of each before it's worked."""
    for item in items:
        if stop():
            return
        if taken is not None:
            taken(item)
        yield item


REPAIR_TYPES = ("probe", "render", "embed", "vision", "faces", "redetect-faces", "ocr", "transcribe", "video-scenes", "scene-vision", "search-sync", "all")
RepairType = Literal["probe", "render", "embed", "vision", "faces", "redetect-faces", "ocr", "transcribe", "video-scenes", "scene-vision", "search-sync", "all"]


class _RepairStats:
    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.processed = 0
        self.failed = 0
        self.skipped = 0


def _make_progress(console: Console) -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        TextColumn("[green]{task.fields[ok]}[/green] ok  [red]{task.fields[fail]}[/red] fail"),
        console=console,
        transient=False,
    )


def _ocr_one(
    *,
    asset_id: str,
    rel_path: str,
    ocr_provider: object,
    proxy_cache: "ProxyCache | None" = None,
) -> dict | None:
    """Run OCR on one asset. Returns {"asset_id", "ocr_text"}, or None when
    there's no proxy or the image can't be prepared. The model's failure
    raises CaptionError (saying whether the endpoint was at fault)."""
    from src.client.workers.captions.base import CaptionError

    import time as _time
    try:
        t0 = _time.perf_counter()
        image_bytes = proxy_cache.get(asset_id, rel_path) if proxy_cache else None
        t_proxy = _time.perf_counter() - t0
        if image_bytes is None:
            return None

        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(image_bytes)
            tmp_path = Path(tmp.name)
        del image_bytes

        t1 = _time.perf_counter()
        try:
            ocr_text = ocr_provider.extract_text(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        t_ocr = _time.perf_counter() - t1

        if ocr_text:
            logger.info("OCR found: %s", ocr_text[:200])
        logger.info("ocr timings: %s — proxy=%.1fms ocr=%.1fms",
                     rel_path, t_proxy * 1000, t_ocr * 1000)
        return {"asset_id": asset_id, "ocr_text": ocr_text or ""}

    except CaptionError:
        raise
    except Exception as e:
        logger.exception("Failed OCR for %s: %s", rel_path, e)
        return None


def default_render_concurrency() -> int:
    """Analysis proxies rendered at once by default: a 4K HEVC decode uses a
    few cores, so one per six, at most three (the storage's bandwidth)."""
    return max(1, min(3, (os.cpu_count() or 1) // 6))


def _probe_one(client: LumiverbClient, lib_root: "Path", asset: dict,
               producers: "ProducerSettings | None" = None,
               fail: "Callable[[str, object], None] | None" = None) -> str:
    """Probe one video's source file and store the facet with how it was
    found (the server's settings when producers isn't given). Returns "ok",
    "missing" or "failed". fail(asset_id, error) hears of a failure (not of
    a file that isn't there)."""
    source = resolve_source_path(lib_root, asset["rel_path"])
    if not source.is_file():
        logger.warning("Source file not found for %s: %s", asset["asset_id"], asset["rel_path"])
        return "missing"
    try:
        facet = probe_video(source)
    except Exception as exc:  # noqa: BLE001 — any ffprobe failure
        logger.warning("Probe failed for %s: %s", asset["rel_path"], exc)
        if fail:
            fail(asset["asset_id"], exc)
        return "failed"
    try:
        if producers is None:  # the server refuses a probe that doesn't say how it was made
            from src.client.cli.producer_settings import ProducerSettings

            producers = ProducerSettings(client)
        body = {**facet.to_dict(), "lineage": producers.lineage("probe", asset.get("sha256"))}
        client.put(f"/v1/assets/{asset['asset_id']}/video-facet", json=body)
    except Exception as exc:  # noqa: BLE001 — e.g. the asset was trashed mid-run
        logger.warning("Storing probe failed for %s: %s", asset["rel_path"], exc)
        if fail:
            fail(asset["asset_id"], exc)
        return "failed"
    return "ok"


def _render_one(
    client: LumiverbClient,
    lib_root: Path,
    asset: dict,
    settings: AnalysisProxySettings,
    cache: AnalysisProxyCache,
    producers: "ProducerSettings | None" = None,
    fail: "Callable[[str, object], None] | None" = None,
) -> str:
    """Render, upload and cache one video's analysis proxy, saying how it was
    made (the server's settings when producers isn't given). Returns "ok",
    "missing" or "failed". fail(asset_id, error) hears of a failure (not of
    a file that isn't there)."""
    source = resolve_source_path(lib_root, asset["rel_path"])
    if not source.is_file():
        logger.warning("Source file not found for %s: %s", asset["asset_id"], asset["rel_path"])
        return "missing"
    # Rendered beside the cache (same filesystem, so put() is a rename),
    # under a name eviction ignores.
    work = cache.path_for(asset["asset_id"]).with_suffix(".rendering")
    work.parent.mkdir(parents=True, exist_ok=True)
    try:
        render_analysis_proxy(source, work, settings, timeout=render_timeout(asset.get("duration_sec")))
        if producers is None:  # the server refuses a copy that doesn't say how it was made
            from src.client.cli.producer_settings import ProducerSettings

            producers = ProducerSettings(client)
        data = {"lineage": json.dumps(producers.lineage("analysis_proxy", asset.get("sha256"),
                                                        used=settings.output()))}
        with open(work, "rb") as f:
            client.post(
                f"/v1/assets/{asset['asset_id']}/artifacts/analysis_proxy",
                files={"file": ("analysis.mp4", f, "video/mp4")},
                data=data,
            )
        cache.put(asset["asset_id"], work)
        return "ok"
    except RenderError as exc:
        logger.warning("Rendering the analysis proxy for %s failed: %s", asset["rel_path"], exc)
        if fail:
            fail(asset["asset_id"], exc)
        return "failed"
    except Exception as exc:  # noqa: BLE001 — e.g. the upload failed or the asset was trashed
        logger.warning("Storing the analysis proxy for %s failed: %s", asset["rel_path"], exc)
        if fail:
            fail(asset["asset_id"], exc)
        return "failed"
    finally:
        work.unlink(missing_ok=True)


def _transcribe_one(
    source_path: "Path",
    transcriber: "Transcriber",
    vad_min_silence_ms: int = 500,
) -> tuple[str, str] | None:
    """Transcribe a video: every audio track mixed into a 16 kHz mono WAV,
    its speech found here with the producer's VAD setting, and only the
    speech heard by whichever of the transcripts job's machines is free
    (transcriber). Times come back onto the clip.

    Returns (srt_text, language); srt_text is "" when nothing is said. None
    when it couldn't be tried this time (the audio tracks couldn't be read,
    finding the speech failed): left missing, tried again later. Raises
    TranscriptError when the machines couldn't hear it (it says whose fault).
    """
    import subprocess
    import tempfile
    from pathlib import Path

    from src.client.workers.transcripts.speech import SpeechError, find_speech, restore, to_srt
    from src.shared.whisper_models import language_code

    try:
        tracks = audio_tracks(source_path)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        logger.warning("Couldn't read the audio tracks of %s; trying again later: %s", source_path, exc)
        return None
    if not tracks:
        logger.info("No audio track in %s", source_path)
        return ("", "")

    with tempfile.TemporaryDirectory(prefix="lumiverb-speech-") as tmp:
        # Every audio track mixed: a lav may be on any track.
        wav, speech = Path(tmp) / "audio.wav", Path(tmp) / "speech.wav"
        try:
            result = subprocess.run(speech_wav_command(source_path, wav, tracks), capture_output=True, timeout=1800)
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning("Reading the audio of %s failed; trying again later: %s", source_path, e)
            return None
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace") if result.stderr else ""
            if "does not contain any stream" in stderr or "Output file #0 does not contain" in stderr:
                logger.info("No audio track in %s", source_path)
                return ("", "")  # deterministic: no audio
            # Anything else isn't known to be silence: ffmpeg killed by a stop
            # (an update, a reboot), the analysis proxy evicted from the cache
            # mid-read, a stream it couldn't decode. Saving "" would say the
            # clip has no speech, for good; it's tried again later instead
            # (and given up on, if it keeps failing).
            logger.warning("Reading the audio of %s failed (ffmpeg exit %s); trying again later: %s",
                           source_path, result.returncode, stderr[:500])
            return None
        if not wav.exists() or wav.stat().st_size < 1000:  # < 1KB = essentially empty
            logger.info("Audio track too short/empty for %s", source_path)
            return ("", "")

        try:
            chunks = find_speech(wav, speech, vad_min_silence_ms)
        except SpeechError as e:
            logger.warning("Couldn't find the speech in %s; trying again later: %s", source_path, e)
            return None
        if not chunks:
            logger.info("No speech detected in %s", source_path.name)
            return ("", "")
        heard = transcriber.transcribe(speech)

    srt_text = to_srt(restore(heard.segments, chunks))
    language = language_code(heard.language)
    if srt_text:
        logger.info("Transcribed %s: %d chars, language=%s", source_path.name, len(srt_text), language)
    else:
        logger.info("No speech detected in %s", source_path.name)
    return (srt_text, language)


def _page_missing(
    client: LumiverbClient,
    library_id: str,
    *,
    missing_vision: bool = False,
    missing_embeddings: bool = False,
    missing_faces: bool = False,
    missing_video_scenes: bool = False,
    missing_ocr: bool = False,
    missing_scene_vision: bool = False,
    missing_transcription: bool = False,
    missing_probe: bool = False,
    missing_analysis_proxy: bool = False,
) -> list[dict]:
    """Page through assets matching the given missing filter, with an
    approved upgrade's clips after what's missing (ADR-016 phase 3)."""
    results: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict[str, str] = {
            "library_id": library_id,
            "limit": "500",
            "sort": "asset_id",
            "dir": "asc",
        }
        if missing_vision:
            params["missing_vision"] = "true"
        if missing_embeddings:
            params["missing_embeddings"] = "true"
        if missing_faces:
            params["missing_faces"] = "true"
        if missing_video_scenes:
            params["missing_video_scenes"] = "true"
        if missing_ocr:
            params["missing_ocr"] = "true"
        if missing_scene_vision:
            params["missing_scene_vision"] = "true"
        if missing_transcription:
            params["missing_transcription"] = "true"
        if missing_probe:
            params["missing_probe"] = "true"
        if missing_analysis_proxy:
            params["missing_analysis_proxy"] = "true"
        if cursor:
            params["after"] = cursor
        resp = client.get("/v1/assets/page", params=params)
        data = resp.json()
        items = data.get("items", [])
        if not items:
            break
        results.extend(items)
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return results


def _page_all_images(
    client: LumiverbClient,
    library_id: str,
) -> list[dict]:
    """Page through ALL image assets in a library (for redetect-faces)."""
    results: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict[str, str] = {
            "library_id": library_id,
            "limit": "500",
            "sort": "asset_id",
            "dir": "asc",
            "media_type": "image",
        }
        if cursor:
            params["after"] = cursor
        resp = client.get("/v1/assets/page", params=params)
        data = resp.json()
        items = data.get("items", [])
        if not items:
            break
        results.extend(items)
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return results


def _repair_embed_one(
    *,
    asset_id: str,
    rel_path: str,
    clip_provider: object,
    proxy_cache: "ProxyCache | None" = None,
) -> dict | None:
    """Generate CLIP embedding for one asset. Returns result dict or None."""
    import time as _time
    t0 = _time.perf_counter()
    image_bytes = proxy_cache.get(asset_id, rel_path) if proxy_cache else None
    t_proxy = _time.perf_counter() - t0
    if image_bytes is None:
        logger.warning("No proxy for %s", rel_path)
        return None

    from PIL import Image as PILImage
    t1 = _time.perf_counter()
    img = PILImage.open(io.BytesIO(image_bytes)).convert("RGB")
    del image_bytes
    vector = clip_provider.embed_image(img)
    img.close()
    del img
    t_embed = _time.perf_counter() - t1

    logger.info("embed timings: %s — proxy=%.1fms embed=%.1fms",
                 rel_path, t_proxy * 1000, t_embed * 1000)

    return {
        "asset_id": asset_id,
        "model_id": clip_provider.model_id,
        "model_version": clip_provider.model_version,
        "vector": vector,
    }


def _face_batch_worker(
    base_url: str,
    token: str,
    batch: list[dict],
    cache_dir: str | None = None,
    lineage: dict | None = None,
) -> dict:
    """Run face detection on a batch of assets in a subprocess.
    lineage: how the faces are found, sent with them (each item's file hash
    its own); the server's settings when not given.

    ONNX Runtime leaks ~35MB per inference call with no fix available.
    Running in a subprocess ensures all native memory is reclaimed by
    the OS when the process exits.

    Returns {"processed": N, "failed": N, "skipped": N}.
    """
    import os
    import sys
    import time as _startup_time
    import warnings
    warnings.filterwarnings("ignore", category=FutureWarning, module="insightface")

    from pathlib import Path

    _t0 = _startup_time.perf_counter()
    sys.stderr.write(f"[face-worker pid={os.getpid()}] starting, {len(batch)} assets\n")
    sys.stderr.flush()

    client = LumiverbClient(base_url=base_url, token=token)
    provider = InsightFaceProvider()
    provider.ensure_loaded()

    _t_load = _startup_time.perf_counter() - _t0
    sys.stderr.write(f"[face-worker pid={os.getpid()}] model loaded in {_t_load:.1f}s\n")
    sys.stderr.flush()

    from PIL import Image as PILImage

    cache_path = Path(cache_dir) if cache_dir else None

    import time as _time
    from concurrent.futures import ThreadPoolExecutor, Future

    _batch_start = _time.perf_counter()
    _cache_hits = 0
    _downloads = 0
    _total_faces = 0

    processed = failed = skipped = 0
    errors: list[dict] = []
    batch_items: list[dict] = []  # items for batch-faces POST

    for item in batch:
        asset_id = item["asset_id"]
        rel_path = item.get("rel_path", asset_id)
        try:
            # Try cache first, then fall back to server download
            image_bytes = None
            if cache_path is not None:
                cached = cache_path / asset_id
                if cached.exists():
                    image_bytes = cached.read_bytes()
                    _cache_hits += 1
            if image_bytes is None:
                _downloads += 1
                resp = client._client.get(client._url(f"/v1/assets/{asset_id}/proxy"))
                if resp.status_code != 200:
                    skipped += 1
                    errors.append({"asset_id": asset_id, "rel_path": rel_path,
                                   "error": f"proxy HTTP {resp.status_code}"})
                    resp.close()
                    continue
                image_bytes = resp.content
                resp.close()
                del resp

            img = PILImage.open(io.BytesIO(image_bytes)).convert("RGB")
            del image_bytes
            detections = provider.detect_faces(img)
            _total_faces += len(detections)
            img.close()
            del img

            batch_items.append({
                "asset_id": asset_id,
                "source_sha256": item.get("sha256"),
                "detection_model": provider.model_id,
                "detection_model_version": provider.model_version,
                # InsightFace's model pack embeds the faces it finds.
                "embedding_model": provider.model_version,
                "faces": [
                    {
                        "bounding_box": d.bounding_box,
                        "detection_confidence": d.detection_confidence,
                        "embedding": d.embedding,
                    }
                    for d in detections
                ],
            })
            del detections
        except Exception as e:
            failed += 1
            errors.append({"asset_id": asset_id, "rel_path": rel_path, "error": str(e)})

    # Single batch POST instead of N individual requests
    if batch_items:
        if lineage is None:  # the server refuses faces that don't say how they were found
            from src.client.cli.producer_settings import ProducerSettings

            lineage = ProducerSettings(client).lineage("faces", None)
        try:
            resp = client.post("/v1/assets/batch-faces", json={"items": batch_items, "lineage": lineage})
            result_data = resp.json()
            processed = result_data.get("processed", 0)
            skipped += result_data.get("skipped", 0)
        except Exception as e:
            # Fallback: submit individually
            sys.stderr.write(f"[face-worker] batch-faces failed ({e}), falling back to individual\n")
            for bi in batch_items:
                try:
                    client.post(f"/v1/assets/{bi['asset_id']}/faces", json={
                        "detection_model": bi["detection_model"],
                        "detection_model_version": bi["detection_model_version"],
                        "embedding_model": bi["embedding_model"],
                        "faces": bi["faces"],
                        "lineage": {**lineage, "source_sha256": bi.get("source_sha256")},
                    })
                    processed += 1
                except Exception as e2:
                    failed += 1
                    errors.append({"asset_id": bi["asset_id"], "rel_path": bi["asset_id"], "error": str(e2)})

    _elapsed = _time.perf_counter() - _batch_start
    return {
        "processed": processed, "failed": failed, "skipped": skipped, "errors": errors,
        "faces_found": _total_faces, "cache_hits": _cache_hits, "downloads": _downloads,
        "elapsed": _elapsed,
    }


def _process_face_result(
    batch_num: int,
    asset_ids: list[str],
    ar: "mp.pool.AsyncResult",
    stats: _RepairStats,
    progress,
    tid,
    console,
    on_fail: "Callable[[str, object], None] | None" = None,
) -> None:
    """Process a single completed face batch result; on_fail(asset_id, error)
    hears of each asset it couldn't do."""
    try:
        result = ar.get()
    except Exception as e:
        console.print(f"[red]Batch {batch_num} failed: {e}[/red]")
        # The batch's process died (the GPU, ONNX, memory): this machine's
        # problem, not the clips', so none is charged; they're tried next run.
        result = {"processed": 0, "failed": len(asset_ids), "skipped": 0, "errors": []}

    _el = result.get("elapsed", 0)
    _n = result["processed"] + result["failed"] + result["skipped"]
    logger.info("faces worker: %d ok, %d fail, %d skip, %d faces, "
                "%d cache/%d download, %.1fs (%.0fms/img)",
                result["processed"], result["failed"], result["skipped"],
                result.get("faces_found", 0), result.get("cache_hits", 0),
                result.get("downloads", 0), _el, (_el / max(_n, 1)) * 1000)

    for err in result.get("errors", []):
        console.print(f"[red]faces \u2717[/red] {err['rel_path']}: {err['error']}")
        if on_fail is not None and err.get("asset_id"):
            on_fail(err["asset_id"], err["error"])

    with stats.lock:
        stats.processed += result["processed"]
        stats.failed += result["failed"]
        stats.skipped += result["skipped"]
        ok, fail = stats.processed, stats.failed
    batch_total = result["processed"] + result["failed"] + result["skipped"]
    progress.advance(tid, batch_total)
    progress.update(tid, ok=ok, fail=fail)


def _collect_face_results(
    inflight: list,
    stats: _RepairStats,
    progress,
    tid,
    console,
    *,
    block: bool = False,
    on_fail: "Callable[[str, object], None] | None" = None,
) -> None:
    """Collect all ready face batch results. If block=True, wait for at least one."""
    # Sweep all ready results
    collected = 0
    i = 0
    while i < len(inflight):
        batch_num, asset_ids, ar = inflight[i]
        if ar.ready():
            inflight.pop(i)
            _process_face_result(batch_num, asset_ids, ar, stats, progress, tid, console, on_fail)
            collected += 1
        else:
            i += 1

    # If nothing was ready and we must block, wait on the oldest
    if collected == 0 and block and inflight:
        batch_num, asset_ids, ar = inflight.pop(0)
        _process_face_result(batch_num, asset_ids, ar, stats, progress, tid, console, on_fail)


def _generate_proxy_for_item(
    item: dict,
    root_path: "Path | None",
    proxy_cache: object,
) -> dict | None:
    """Generate a proxy for a single asset item. Returns the item or None if skipped.

    Runs in a thread pool to pipeline proxy generation with face detection.
    Skips generation if the proxy is already in the persistent cache.
    """
    from pathlib import Path
    from src.client.proxy.proxy_gen import generate_face_proxy

    asset_id = item["asset_id"]
    rel_path = item.get("rel_path", asset_id)
    expected_hash = item.get("sha256")

    # Check persistent cache: proxy exists AND source hash matches
    if proxy_cache.has(asset_id):
        sha_file = proxy_cache.path / f"{asset_id}.sha"
        if expected_hash and sha_file.exists():
            cached_hash = sha_file.read_text().strip()
            if cached_hash == expected_hash:
                return item  # cache hit — source unchanged
            # Stale — source changed, regenerate below
        elif not expected_hash:
            return item  # no hash to check, trust the cache

    if root_path is not None:
        from src.shared.io_utils import resolve_source_path

        source = resolve_source_path(root_path, rel_path)
        if source.is_file():
            if expected_hash:
                from src.client.workers.exif_extract import compute_sha256
                local_hash = compute_sha256(source)
                if local_hash != expected_hash:
                    return None  # SHA mismatch — skip
            try:
                jpeg_bytes = generate_face_proxy(source)
                proxy_cache.put(asset_id, jpeg_bytes)
                if expected_hash:
                    (proxy_cache.path / f"{asset_id}.sha").write_text(expected_hash)
                del jpeg_bytes
            except Exception:
                pass  # fall back to server download in worker

    return item


def _run_face_pipeline(
    *,
    assets: list[dict],
    client: "LumiverbClient",
    proxy_cache: object,
    root_path: "Path | None",
    face_conc: int,
    batch_size: int,
    batch_limit: int,
    stats: _RepairStats,
    progress,
    tid,
    console,
    label: str = "faces",
    lineage: dict | None = None,
    on_fail: "Callable[[str, object], None] | None" = None,
) -> None:
    """Pipeline proxy generation → face detection → result collection.

    Proxy generation runs in a thread pool; face detection in a subprocess pool.
    A queue connects them: proxy threads put ready items, main thread consumes
    them into batches and dispatches to detection workers.
    """
    import queue
    import threading
    from concurrent.futures import ThreadPoolExecutor

    ctx = mp.get_context("spawn")
    proxy_threads = max(face_conc * 2, 4)  # I/O bound, can oversubscribe
    console.print(f"[dim]{label}: {face_conc} detect workers, {proxy_threads} proxy threads[/dim]")

    pool = ctx.Pool(face_conc, initializer=_silence_subprocess_stdout, maxtasksperchild=batch_limit)

    # Warm up detection workers — CoreML model compilation takes ~50s on first load
    console.print("[dim]Warming up face detection model (first load may take a minute)...[/dim]")
    _warmup = pool.apply_async(_face_batch_worker, (client.base_url, client.token, [], None))
    _warmup.get()  # blocks until model is loaded
    console.print("[dim]Model ready.[/dim]")

    proxy_pool = ThreadPoolExecutor(max_workers=proxy_threads, thread_name_prefix="proxy-gen")
    ready_q: queue.Queue = queue.Queue()
    _SENTINEL = None

    skipped = 0
    skip_lock = threading.Lock()

    def _proxy_worker(item: dict) -> None:
        nonlocal skipped
        result = _generate_proxy_for_item(item, root_path, proxy_cache)
        if result is None:
            with skip_lock:
                skipped += 1
            with stats.lock:
                stats.skipped += 1
            # Advance progress for skipped items from main thread via queue
            ready_q.put(("skip", None))
        else:
            ready_q.put(("item", result))

    def _submit_all() -> None:
        """Submit all proxy jobs, then signal done."""
        futures = [proxy_pool.submit(_proxy_worker, item) for item in assets]
        # Wait for all proxy gen to finish
        for f in futures:
            f.result()  # propagate exceptions
        ready_q.put(("done", None))

    # Start proxy generation in background thread
    feeder = threading.Thread(target=_submit_all, daemon=True)
    feeder.start()

    try:
        inflight: list[tuple[int, int, mp.pool.AsyncResult]] = []
        batch_buf: list[dict] = []
        batch_num = 0

        while True:
            # Sweep any ready detection results (non-blocking)
            if inflight:
                _collect_face_results(inflight, stats, progress, tid, console, on_fail=on_fail)

            # Get next item from proxy queue (short timeout so we keep sweeping)
            try:
                msg_type, item = ready_q.get(timeout=0.1)
            except queue.Empty:
                continue

            if msg_type == "done":
                break
            elif msg_type == "skip":
                progress.advance(tid, 1)
                continue

            batch_buf.append(item)

            if len(batch_buf) >= batch_size:
                batch_num += 1
                logger.info("%s batch %d: %d assets", label, batch_num, len(batch_buf))
                ar = pool.apply_async(
                    _face_batch_worker,
                    (client.base_url, client.token, batch_buf, str(proxy_cache.path), lineage),
                )
                inflight.append((batch_num, [b["asset_id"] for b in batch_buf], ar))
                batch_buf = []

                # If too many inflight, block until one finishes
                while len(inflight) >= face_conc * 2:
                    _collect_face_results(inflight, stats, progress, tid, console, block=True, on_fail=on_fail)

        # Flush remaining partial batch
        if batch_buf:
            batch_num += 1
            logger.info("%s batch %d: %d assets (final)", label, batch_num, len(batch_buf))
            ar = pool.apply_async(
                _face_batch_worker,
                (client.base_url, client.token, batch_buf, str(proxy_cache.path), lineage),
            )
            inflight.append((batch_num, [b["asset_id"] for b in batch_buf], ar))

        # Drain all remaining detection results
        while inflight:
            _collect_face_results(inflight, stats, progress, tid, console, block=True, on_fail=on_fail)

        feeder.join(timeout=5)

        if skipped:
            console.print(f"[dim]{skipped} assets skipped (SHA mismatch)[/dim]")
    finally:
        pool.close()
        pool.join()
        proxy_pool.shutdown(wait=False)


def _here(library: dict) -> str:
    """The library's root as this machine sees it, for messages."""
    from src.client.cli.roots import local_library_root

    return str(local_library_root(library) or library.get("root_path") or "")


def get_repair_summary(client: LumiverbClient, library_id: str) -> dict:
    """Fetch repair summary counts from the API."""
    resp = client.get("/v1/assets/repair-summary", params={"library_id": library_id})
    return resp.json()


def run_repair(
    client: LumiverbClient,
    library: dict,
    *,
    job_type: RepairType = "all",
    dry_run: bool = False,
    concurrency: int = 4,
    force: bool = False,
    console: Console,
    asset_ids: list[str] | None = None,
    skip_types: set[str] | None = None,
    should_stop: Callable[[], bool] | None = None,
    skip_items: Collection[tuple[str, str]] = (),
    on_take: Callable[[str, str], None] | None = None,
    render_alongside: bool = False,
) -> None:
    """Detect and fix missing pipeline outputs.

    render_alongside: render analysis proxies in a thread beside the other
    steps instead of before them (the worker does: see _render_step).

    If asset_ids is provided, only those assets are considered for enrichment.
    This is used by Phase 3 (ingest convergence) to pass scanned asset IDs
    directly, avoiding redundant re-paging of the entire library.

    If skip_types is provided, those enrichment types are excluded from the
    plan even when job_type="all". Used by ingest to honor --skip-vision
    and --skip-embeddings.

    If should_stop is provided, it's asked between items: once it says
    stop, the current step ends after the item in hand and no other starts.
    The brain's worker uses it to give enrichment a time budget.

    skip_items are (step, asset_id) pairs left for a later run, and
    on_take(step, asset_id) hears of each item a step takes. The worker
    uses them so an item that keeps failing doesn't hold up the rest.
    """
    library_id = library["library_id"]
    library_name = library["name"]

    # Filter helper: when asset_ids is set, restrict to those IDs only.
    # Defined up here (rather than next to the per-type repair loop below)
    # because the redetect-faces plan-building branch already needs to
    # filter the page result before the plan is even built. Defining the
    # closure later turned `_filter` into an unbound local at line 759 and
    # crashed `lumiverb repair --job-type redetect-faces` on entry.
    _id_set = set(asset_ids) if asset_ids else None

    def _filter(assets: list[dict]) -> list[dict]:
        return [a for a in assets if a["asset_id"] in _id_set] if _id_set else assets

    # Step 1: Get summary
    console.print(f"[bold]Checking library: {library_name}[/bold]")
    summary = get_repair_summary(client, library_id)

    # Build repair plan
    plan: list[tuple[str, int, str]] = []  # (type, count, description)
    # Probe first: its duration makes videos eligible for transcription and scenes.
    if job_type in ("probe", "all") and summary.get("missing_probe", 0) > 0:
        plan.append(("probe", summary["missing_probe"], "missing video probe"))
    # Then the analysis proxies that transcription, scenes and vision read.
    if job_type in ("render", "all") and summary.get("missing_analysis_proxy", 0) > 0:
        plan.append(("render", summary["missing_analysis_proxy"], "missing analysis proxy"))

    if job_type in ("embed", "all") and summary.get("missing_embeddings", 0) > 0:
        plan.append(("embed", summary["missing_embeddings"], "missing CLIP embeddings"))
    if job_type in ("vision", "all") and summary.get("missing_vision", 0) > 0:
        plan.append(("vision", summary["missing_vision"], "missing AI descriptions"))
    if job_type in ("faces", "all") and summary.get("missing_faces", 0) > 0:
        plan.append(("faces", summary["missing_faces"], "missing face detection"))
    if job_type == "redetect-faces":
        # Count ALL images, not just missing — this re-runs detection on everything
        all_images = _filter(_page_all_images(client, library_id))
        if all_images:
            plan.append(("redetect-faces", len(all_images), "re-detect faces (all images)"))
    if job_type in ("ocr", "all") and summary.get("missing_ocr", 0) > 0:
        plan.append(("ocr", summary["missing_ocr"], "missing OCR text"))
    if job_type in ("transcribe", "all") and summary.get("missing_transcription", 0) > 0:
        plan.append(("transcribe", summary["missing_transcription"], "missing transcription"))
    if job_type in ("video-scenes", "all") and summary.get("missing_video_scenes", 0) > 0:
        plan.append(("video-scenes", summary["missing_video_scenes"], "missing video scene detection"))
    if job_type in ("scene-vision", "all") and summary.get("missing_scene_vision", 0) > 0:
        plan.append(("scene-vision", summary["missing_scene_vision"], "missing scene vision AI"))
    if job_type in ("search-sync", "all"):
        stale = summary.get("stale_search_sync", 0)
        if force:
            plan.append(("search-sync", summary.get("total_assets", 0), "full re-index (--force)"))
        elif stale > 0:
            plan.append(("search-sync", stale, "stale search index"))

    # Apply skip_types filter (used by ingest --skip-vision / --skip-embeddings)
    if skip_types:
        plan = [(t, c, d) for t, c, d in plan if t not in skip_types]

    # Display summary table
    table = Table(title=f"Repair Summary — {library_name}", show_lines=False)
    table.add_column("Category", style="bold")
    table.add_column("Count", justify="right")
    table.add_column("Status")

    total = summary.get("total_assets", 0)
    table.add_row("Total assets", str(total), "")

    for label, key, needs_repair in [
        ("Video probe", "missing_probe", job_type in ("probe", "all")),
        ("Analysis proxy", "missing_analysis_proxy", job_type in ("render", "all")),
        ("Proxy", "missing_proxy", job_type in ("proxy", "all")),
        ("EXIF", "missing_exif", job_type in ("exif", "all")),
        ("Embeddings", "missing_embeddings", job_type in ("embed", "all")),
        ("Vision AI", "missing_vision", job_type in ("vision", "all")),
        ("Faces", "missing_faces", job_type in ("faces", "all")),
        ("OCR", "missing_ocr", job_type in ("ocr", "all")),
        ("Transcription", "missing_transcription", job_type in ("transcribe", "all")),
        ("Video scenes", "missing_video_scenes", job_type in ("video-scenes", "all")),
        ("Scene vision", "missing_scene_vision", job_type in ("scene-vision", "all")),
        ("Search sync", "stale_search_sync", job_type in ("search-sync", "all")),
    ]:
        count = summary.get(key, 0)
        if count == 0:
            status = "[green]✓ complete[/green]"
        elif needs_repair:
            status = f"[yellow]⚠ {count} to repair[/yellow]"
        else:
            status = f"[dim]{count} missing[/dim]"
        table.add_row(label, str(count), status)

    console.print(table)

    if not plan:
        console.print("\n[green]Nothing to repair.[/green]")
        return

    if dry_run:
        console.print("\n[dim]--dry-run: no changes made.[/dim]")
        return

    # Step 2: Execute repairs in logical order
    stats = _RepairStats()

    # Load concurrency config: --concurrency flag acts as max, per-type defaults are lower for GPU ops
    from src.client.cli.config import load_config as _load_cfg
    _cfg = _load_cfg()
    # What each producer makes its artifact with now: the server's settings,
    # so what's recorded in lineage is current.
    from src.client.cli.producer_settings import ProducerSettings
    producers = ProducerSettings(client)
    # What a step couldn't make goes to the server, which waits before
    # handing it out again (5 minutes, doubling up to a day).
    from src.client.cli.failure_report import FailureReport
    from src.client.cli.vision_guard import VisionGuard
    failures = FailureReport(client)
    # Vision steps run only while a machine doing vision offers the account's model.
    vision = VisionGuard(client, failures)
    # Transcription only while a machine doing transcripts can (the built-in Whisper, or a server).
    from src.client.cli.transcript_guard import TranscriptGuard
    transcripts = TranscriptGuard(client, failures)

    def _vision_ready() -> bool:
        if vision.check():
            console.print(f"  Vision AI: {vision.describe()}")
            return True
        console.print(f"[yellow]  Vision AI can't be used: {vision.error}[/yellow]")
        return False

    def _transcripts_ready() -> bool:
        if transcripts.check():
            console.print(f"  Transcripts: {transcripts.describe()}")
            return True
        console.print(f"[yellow]  Transcripts wait: {transcripts.error}[/yellow]")
        return False
    max_conc = min(concurrency, _cfg.max_concurrency)
    embed_conc = min(max_conc, concurrency)  # embed is CPU-bound (CLIP), full concurrency
    # Face detection uses subprocess isolation (ONNX memory leak). On macOS,
    # multiple workers loading CoreML models simultaneously can hang. Default
    # to 1 worker; concurrency applies to proxy generation threads instead.
    face_conc = 1

    # The library's root on this machine (mapped by `lumiverb config
    # map-root`), or None when it can't be read now. Checked again before
    # each step that reads source files: storage can go to sleep mid-run.
    from src.client.cli.roots import reachable_root
    root_path = reachable_root(library)

    # Shared proxy cache: generates from local source → server download → cached at configured size
    from src.client.proxy.proxy_cache import ProxyCache
    # The images CLIP, faces and vision see: CLIP's input size (the registry's).
    proxy_cache = ProxyCache(max_edge=PROXY_CACHE_EDGE, root_path=root_path, client=client)
    # Videos are analyzed from their analysis proxies, never the originals.
    from src.client.proxy.analysis_cache import AnalysisProxyCache
    analysis_cache = AnalysisProxyCache(client)

    def _waiting_for_proxy(assets: list[dict]) -> list[dict]:
        """Split off videos without an analysis proxy yet, and say so."""
        waiting = [a for a in assets if not a.get("has_analysis_proxy")]
        if waiting:
            console.print(
                f"  {len(waiting):,} wait for an analysis proxy "
                f"(rendered while the library's storage is reachable)."
            )
        return [a for a in assets if a.get("has_analysis_proxy")]

    stop = should_stop or (lambda: False)

    def _due(step: str, assets: list[dict]) -> list[dict]:
        """assets, less those skip_items leaves for a later run."""
        left = [a for a in assets if (step, a["asset_id"]) not in skip_items]
        if len(left) < len(assets):
            console.print(f"  {len(assets) - len(left):,} tried recently; they wait for a later run.")
        return left

    def _taking(step: str) -> Callable[[dict], None] | None:
        """For _until: tells on_take of each asset a step takes."""
        return None if on_take is None else lambda a: on_take(step, a["asset_id"])

    def _asleep() -> bool:
        """After a missing file: is the storage gone, rather than the file?"""
        if reachable_root(library, require_entries=True) is not None:
            return False
        console.print(f"[yellow]Library root stopped answering: {_here(library)}. "
                      "The rest waits until it's reachable.[/yellow]")
        return True

    def _render_step(count: int, desc: str, console: Console = console) -> None:
        """Render analysis proxies (needs the originals' storage)."""
        console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")

        # Rendering reads the originals, so it waits for their storage.
        lib_root = reachable_root(library)
        if lib_root is None:
            console.print(f"[yellow]Library root not accessible: {_here(library)}[/yellow]")
            console.print("[yellow]Analysis proxies are rendered once it's reachable. Skipping.[/yellow]")
            return

        assets = _filter(_page_missing(client, library_id, missing_analysis_proxy=True))
        if not assets:
            console.print("No assets found (already rendered?).")
            return
        assets = _due("render", assets)

        # The account's settings, so what's recorded as current is what's
        # rendered; the encoder and decoder are this machine's.
        settings = AnalysisProxySettings.for_producer(producers.settings("analysis_proxy"),
                                                      _cfg.analysis_proxy_encoder, _cfg.analysis_proxy_decoder,
                                                      _cfg.gpu_decodes)
        hwaccel = gpu_decoder(settings.decoder) if settings.gpu_decodes > 0 else None
        if hwaccel:
            console.print(f"Decoding on the GPU ({hwaccel}), {settings.gpu_decodes} at a time while it has room; "
                          "the CPU takes the rest.")
        # Several at once: one render of a 4K HEVC original decodes on a few
        # cores and leaves the rest idle.
        render_conc = _cfg.render_concurrency or default_render_concurrency()
        progress = _make_progress(console)
        with progress:
            tid = progress.add_task("Render", total=len(assets), ok=0, fail=0)
            asleep = False

            def _rendered(fut: Future) -> None:
                nonlocal asleep
                try:
                    outcome = fut.result()
                except Exception:  # noqa: BLE001 — _render_one reports its own failures
                    logger.exception("render: failed")
                    outcome = "failed"
                with stats.lock:
                    if outcome == "ok":
                        stats.processed += 1
                    elif outcome == "missing":
                        stats.skipped += 1
                    else:
                        stats.failed += 1
                progress.advance(tid, 1)
                progress.update(tid, ok=stats.processed, fail=stats.failed)
                if outcome == "missing" and not asleep and _asleep():
                    asleep = True

            with ThreadPoolExecutor(max_workers=render_conc, thread_name_prefix="render") as pool:
                inflight: set[Future] = set()
                for a in _until(lambda: stop() or asleep, assets, _taking("render")):
                    inflight.add(pool.submit(_render_one, client, lib_root, a, settings, analysis_cache,
                                             producers, failures.for_artifact("analysis_proxy")))
                    if len(inflight) >= render_conc:
                        done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                        for f in done:
                            _rendered(f)
                for f in as_completed(inflight):
                    _rendered(f)

    # The worker renders alongside the other steps: rendering is CPU work on
    # the originals, the rest mostly the GPU on proxies, and one long render
    # queue in front would hold every other step back for days.
    render_thread: threading.Thread | None = None
    if render_alongside and not dry_run and any(rt == "render" for rt, _, _ in plan):
        render = next(p for p in plan if p[0] == "render")
        plan = [p for p in plan if p[0] != "render"]

        def _render_in_background() -> None:
            # Its own console on the same output: one live progress display each.
            out = Console(file=console.file, force_terminal=console.is_terminal, width=console.width,
                          quiet=console.quiet)
            try:
                _render_step(render[1], render[2], out)
            except Exception:  # noqa: BLE001 — the other steps go on; the next run tries again
                logger.exception("render: failed alongside the other steps")

        render_thread = threading.Thread(target=_render_in_background, name="render", daemon=True)
        render_thread.start()

    for repair_type, count, desc in plan:
        failures.flush()  # the step before's
        if stop():
            console.print("[yellow]Stopping here; the rest waits for the next run.[/yellow]")
            break
        if repair_type == "embed":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")
            try:
                from src.client.workers.embeddings.clip_provider import CLIPEmbeddingProvider
                clip_set = producers.settings("clip")
                clip_provider = CLIPEmbeddingProvider(model_name=clip_set["model"], pretrained=clip_set["pretrained"])
                # The images CLIP sees are the proxy cache's: its size is what was used.
                clip_used = {**clip_set, "input_edge": PROXY_CACHE_EDGE}
                console.print(f"CLIP model: {clip_provider.model_version}")
            except Exception as e:
                console.print(f"[red]Cannot load CLIP model: {e}[/red]")
                continue

            assets = _filter(_page_missing(client, library_id, missing_embeddings=True))
            if not assets:
                console.print("No assets found (already repaired?).")
                continue
            assets = _due("embed", assets)
            embed_sha = {a["asset_id"]: a.get("sha256") for a in assets}

            EMBED_BATCH_SIZE = 50
            embed_batch: list[dict] = []

            def _flush_embed_batch() -> None:
                if not embed_batch:
                    return
                for item in embed_batch:
                    item["source_sha256"] = embed_sha.get(item["asset_id"])
                    item["lineage"] = producers.lineage("clip", item["source_sha256"], used=clip_used)
                try:
                    client.post("/v1/assets/batch-embeddings", json={
                        "items": [{k: v for k, v in item.items() if k != "lineage"} for item in embed_batch],
                        "lineage": producers.lineage("clip", None, used=clip_used),
                    })
                    logger.info("embed batch POST: %d items", len(embed_batch))
                except Exception as e:
                    logger.warning("embed batch POST failed (%d items): %s", len(embed_batch), e)
                    for item in embed_batch:
                        try:
                            client.post(f"/v1/assets/{item['asset_id']}/embeddings", json=item)
                        except Exception:
                            pass
                embed_batch.clear()

            embed_of: dict[Future, str] = {}

            def _collect_embed(done: set[Future]) -> None:
                for f in done:
                    asset_id = embed_of.pop(f)
                    try:
                        result = f.result()
                        error: object = "no embedding"
                    except Exception as e:
                        result, error = None, e
                    if result is not None:
                        embed_batch.append(result)
                        with stats.lock:
                            stats.processed += 1
                    else:
                        failures.add("clip", asset_id, error)
                        with stats.lock:
                            stats.failed += 1
                    progress.advance(tid, 1)
                    with stats.lock:
                        progress.update(tid, ok=stats.processed, fail=stats.failed)
                    if len(embed_batch) >= EMBED_BATCH_SIZE:
                        _flush_embed_batch()
                if (stats.processed + stats.failed) % 10 == 0:
                    gc.collect()

            progress = _make_progress(console)
            with progress:
                tid = progress.add_task("Embeddings", total=len(assets), ok=0, fail=0)
                pool = ThreadPoolExecutor(max_workers=embed_conc, thread_name_prefix="embed")
                inflight: set[Future] = set()
                for a in _until(stop, assets, _taking("embed")):
                    fut = pool.submit(
                        _repair_embed_one,
                        asset_id=a["asset_id"],
                        rel_path=a["rel_path"],
                        clip_provider=clip_provider,
                        proxy_cache=proxy_cache,
                    )
                    embed_of[fut] = a["asset_id"]
                    inflight.add(fut)
                    if len(inflight) >= embed_conc * 2:
                        done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                        _collect_embed(done)
                while inflight:
                    done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                    _collect_embed(done)
                pool.shutdown(wait=True)
                _flush_embed_batch()

        elif repair_type == "vision":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")
            if not _vision_ready():
                continue
            from src.client.cli.ingest import run_backfill_vision
            # As many at once as the online machines take together (Settings → AI).
            run_backfill_vision(
                client, library, concurrency=vision.capacity(), console=console,
                should_stop=lambda: stop() or vision.down,
                skip={asset_id for step, asset_id in skip_items if step == "vision"},
                on_take=None if on_take is None else lambda asset_id: on_take("vision", asset_id),
                producers=producers,
                on_fail=vision.on_fail("vision"),
                provider=vision.provider(settings=producers.with_model("vision", vision.model),
                                         ocr_settings=producers.with_model("ocr", vision.model)),
                model=vision.model,
            )

        elif repair_type == "ocr":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")
            if not _vision_ready():
                continue
            # The model the guard just read, which may be newer than the run's settings.
            ocr_settings = producers.with_model("ocr", vision.model)
            ocr_provider = vision.provider(settings=producers.with_model("vision", vision.model),
                                           ocr_settings=ocr_settings)
            ocr_fail = vision.on_fail("ocr")

            assets = _filter(_page_missing(client, library_id, missing_ocr=True))
            if not assets:
                console.print("No assets found (already repaired?).")
                continue
            assets = _due("ocr", assets)
            ocr_sha = {a["asset_id"]: a.get("sha256") for a in assets}

            import time as _time
            ocr_batch_size = _cfg.ocr_batch_size
            batch_buf: list[dict] = []

            def _flush_ocr_batch():
                if not batch_buf:
                    return
                t0 = _time.perf_counter()
                for item in batch_buf:
                    item["source_sha256"] = ocr_sha.get(item["asset_id"])
                try:
                    client.post("/v1/assets/batch-ocr", json={"items": list(batch_buf), "model_id": vision.model,
                                                              "lineage": producers.lineage("ocr", None,
                                                                                           used=ocr_settings)})
                    t_post = _time.perf_counter() - t0
                    logger.info("ocr batch POST: %d items in %.1fms", len(batch_buf), t_post * 1000)
                except Exception as e:
                    logger.warning("ocr batch POST failed (%d items): %s", len(batch_buf), e)
                    # Fallback: post individually
                    for item in batch_buf:
                        try:
                            client.post(f"/v1/assets/{item['asset_id']}/ocr", json={
                                "ocr_text": item["ocr_text"], "model_id": vision.model,
                                "lineage": producers.lineage("ocr", item.get("source_sha256"), used=ocr_settings)})
                        except Exception:
                            pass
                batch_buf.clear()

            from src.client.workers.captions.base import CaptionError

            def _ocr(a: dict) -> tuple[dict, dict | None, object]:
                try:
                    return a, _ocr_one(asset_id=a["asset_id"], rel_path=a["rel_path"],
                                       ocr_provider=ocr_provider, proxy_cache=proxy_cache), None
                except CaptionError as e:
                    return a, None, e

            def _ocr_done(fut: Future) -> None:
                a, result, ocr_error = fut.result()
                if result is not None:
                    batch_buf.append(result)
                    with stats.lock:
                        stats.processed += 1
                else:
                    ocr_fail(a["asset_id"], ocr_error or
                             "no OCR result (no proxy, or the image couldn't be read: see the log)")
                    with stats.lock:
                        stats.skipped += 1
                if len(batch_buf) >= ocr_batch_size:
                    _flush_ocr_batch()
                with stats.lock:
                    ok, fail = stats.processed, stats.failed
                progress.advance(tid, 1)
                progress.update(tid, ok=ok, fail=fail)

            # As many at once as the online machines take together (Settings → AI);
            # results are gathered here, so the batch is this thread's alone.
            ocr_conc = vision.capacity()
            progress = _make_progress(console)
            with progress:
                tid = progress.add_task("OCR", total=len(assets), ok=0, fail=0)
                with ThreadPoolExecutor(max_workers=ocr_conc, thread_name_prefix="ocr") as ocr_pool:
                    inflight: set[Future] = set()
                    for a in _until(lambda: stop() or vision.down, assets, _taking("ocr")):
                        inflight.add(ocr_pool.submit(_ocr, a))
                        if len(inflight) >= ocr_conc:
                            done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                            for f in done:
                                _ocr_done(f)
                    for f in as_completed(inflight):
                        _ocr_done(f)

                _flush_ocr_batch()  # flush remaining

        elif repair_type == "faces":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")

            assets = _filter(_page_missing(client, library_id, missing_faces=True))
            if not assets:
                console.print("No assets found (already repaired?).")
                continue
            assets = _due("faces", assets)
            if not assets:
                continue

            def _take_chunk(chunk: list[dict]) -> None:
                for a in chunk:
                    on_take("faces", a["asset_id"])

            _face_root = reachable_root(library)

            from src.client.cli.config import load_config
            cfg = load_config()

            chunks = [assets] if should_stop is None else [
                assets[i:i + FACE_CHUNK] for i in range(0, len(assets), FACE_CHUNK)
            ]
            progress = _make_progress(console)
            with progress:
                tid = progress.add_task("Faces", total=len(assets), ok=0, fail=0)
                for chunk in _until(stop, chunks, _take_chunk if on_take else None):
                    _run_face_pipeline(
                        assets=chunk,
                        client=client,
                        proxy_cache=proxy_cache,
                        root_path=_face_root,
                        face_conc=face_conc,
                        batch_size=cfg.face_batch_size,
                        batch_limit=cfg.face_batch_limit,
                        stats=stats,
                        progress=progress,
                        tid=tid,
                        console=console,
                        label="faces",
                        lineage=producers.lineage("faces", None),
                        on_fail=failures.for_artifact("faces"),
                    )

        elif repair_type == "redetect-faces":
            console.print(f"\n[bold]Re-detecting faces on all images ({count})[/bold]")
            console.print("[dim]Person centroids preserved — faces will auto-reassign.[/dim]")

            assets = all_images  # noqa: F821 — bound in plan phase above

            _face_root = reachable_root(library)

            from src.client.cli.config import load_config
            cfg = load_config()

            progress = _make_progress(console)
            with progress:
                tid = progress.add_task("Re-detect faces", total=len(assets), ok=0, fail=0)
                _run_face_pipeline(
                    assets=assets,
                    client=client,
                    proxy_cache=proxy_cache,
                    root_path=_face_root,
                    face_conc=face_conc,
                    batch_size=cfg.face_batch_size,
                    batch_limit=cfg.face_batch_limit,
                    stats=stats,
                    progress=progress,
                    tid=tid,
                    console=console,
                    label="redetect-faces",
                    lineage=producers.lineage("faces", None),
                    on_fail=failures.for_artifact("faces"),
                )

            # Clean up dismissed people left with zero face matches
            try:
                resp = client.post("/v1/upkeep/cleanup-dismissed")
                deleted = resp.json().get("deleted", 0)
                if deleted:
                    console.print(f"[dim]Cleaned up {deleted} empty dismissed people.[/dim]")
            except Exception as e:
                logger.warning("cleanup-dismissed failed: %s", e)

        elif repair_type == "probe":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")

            assets = _filter(_page_missing(client, library_id, missing_probe=True))
            if not assets:
                console.print("No assets found (already probed?).")
                continue
            assets = _due("probe", assets)

            # Probing reads source files, not proxies
            lib_root = reachable_root(library)
            if lib_root is None:
                console.print(f"[yellow]Library root not accessible: {_here(library)}[/yellow]")
                console.print("[yellow]Probing requires source video files. Skipping.[/yellow]")
                continue

            progress = _make_progress(console)
            with progress:
                tid = progress.add_task("Probe", total=len(assets), ok=0, fail=0)
                for a in _until(stop, assets, _taking("probe")):
                    outcome = _probe_one(client, lib_root, a, producers, failures.for_artifact("probe"))
                    with stats.lock:
                        if outcome == "ok":
                            stats.processed += 1
                        elif outcome == "missing":
                            stats.skipped += 1
                        else:
                            stats.failed += 1
                    progress.advance(tid, 1)
                    progress.update(tid, ok=stats.processed, fail=stats.failed)
                    if outcome == "missing" and _asleep():
                        break

        elif repair_type == "render":
            _render_step(count, desc)

        elif repair_type == "transcribe":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")
            if not _transcripts_ready():
                continue

            assets = _filter(_page_missing(client, library_id, missing_transcription=True))
            if not assets:
                console.print("No assets found (already transcribed?).")
                continue
            assets = _due("transcribe", assets)
            # Transcription reads the analysis proxy, so it runs while the
            # originals' storage sleeps.
            assets = _waiting_for_proxy(assets)
            if not assets:
                continue

            from src.client.workers.transcripts.base import TranscriptError

            # The silences skipped are the producer's; the model is the one the
            # guard just read, which may be newer than the run's settings.
            vad_ms = producers.settings("transcript")["vad_min_silence_ms"]
            transcript_settings = producers.with_model("transcript", transcripts.model)
            transcriber = transcripts.transcriber()
            transcript_fail = transcripts.on_fail("transcript")

            def _hear(a: dict) -> tuple[dict, tuple[str, str] | str | None, TranscriptError | None]:
                source_path = analysis_cache.get(a["asset_id"])
                if source_path is None:
                    return a, "no proxy", None
                try:
                    return a, _transcribe_one(source_path, transcriber, vad_ms), None
                except TranscriptError as e:
                    return a, None, e
                except Exception:  # noqa: BLE001 — one clip's surprise doesn't stop the step
                    logger.exception("Transcribing %s failed", a["rel_path"])
                    return a, None, None

            def _heard(fut: Future) -> None:
                a, result, error = fut.result()
                asset_id = a["asset_id"]
                if result == "no proxy":
                    logger.warning("No analysis proxy for %s: %s", asset_id, a["rel_path"])
                    with stats.lock:
                        stats.skipped += 1
                elif error is not None:
                    # The machines' trouble stops transcription, charging no clip;
                    # the clip's is charged once a check finds the machine fine.
                    transcript_fail(asset_id, error)
                    with stats.lock:
                        if error.endpoint_fault:
                            stats.skipped += 1
                        else:
                            stats.failed += 1
                elif result is None:
                    # Left missing, and tried again once its turn comes.
                    failures.add("transcript", asset_id, "transcription failed (see the log)")
                    with stats.lock:
                        stats.failed += 1
                else:
                    srt_text, language = result
                    # Submit transcript (empty string = no speech, sets has_transcript=false)
                    try:
                        client.post(
                            f"/v1/assets/{asset_id}/transcript",
                            json={"srt": srt_text, "language": language, "source": "whisper",
                                  "lineage": producers.lineage("transcript", a.get("sha256"),
                                                               used=transcript_settings)},
                        )
                        with stats.lock:
                            stats.processed += 1
                    except Exception as e:
                        logger.warning("Failed to submit transcript for %s: %s", asset_id, e)
                        failures.add("transcript", asset_id, e)
                        with stats.lock:
                            stats.failed += 1
                with stats.lock:
                    ok_count, fail_count = stats.processed, stats.failed
                progress.advance(tid, 1)
                progress.update(tid, ok=ok_count, fail=fail_count)

            # Twice as many as the online machines take together (Settings → AI): each
            # clip's audio and speech are got ready while the machines work on others
            # (the pool holds each machine to its own limit). Results are posted from
            # here, one at a time.
            transcribe_conc = 2 * transcripts.capacity()
            progress = _make_progress(console)
            with progress:
                tid = progress.add_task("Transcribe", total=len(assets), ok=0, fail=0)
                with ThreadPoolExecutor(max_workers=transcribe_conc, thread_name_prefix="transcribe") as hear_pool:
                    inflight: set[Future] = set()
                    for a in _until(lambda: stop() or transcripts.down, assets, _taking("transcribe")):
                        inflight.add(hear_pool.submit(_hear, a))
                        if len(inflight) >= transcribe_conc:
                            done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                            for f in done:
                                _heard(f)
                    for f in as_completed(inflight):
                        _heard(f)

        elif repair_type == "video-scenes":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")

            assets = _filter(_page_missing(client, library_id, missing_video_scenes=True))
            if not assets:
                console.print("No assets found (already repaired?).")
                continue
            assets = _due("video-scenes", assets)
            # Scenes are found in the analysis proxy, so this runs while the
            # originals' storage sleeps.
            assets = _waiting_for_proxy(assets)
            if not assets:
                continue

            videos = [
                {"asset_id": a["asset_id"], "rel_path": a["rel_path"], "duration_sec": a.get("duration_sec"),
                 "sha256": a.get("sha256")}
                for a in assets
            ]
            indexable = [v for v in videos if v.get("duration_sec")]
            if not indexable:
                console.print("No videos with known duration found.")
                continue

            from src.client.cli.video_index import run_video_index
            progress = _make_progress(console)
            with progress:
                tid = progress.add_task("Scenes", total=len(indexable), ok=0, fail=0)
                done, failed = run_video_index(
                    client=client,
                    source_for=lambda v: analysis_cache.get(v["asset_id"]),
                    videos=_until(stop, indexable, _taking("video-scenes")),
                    console=console,
                    progress=progress,
                    task_id=tid,
                    lineage_for=lambda v: producers.lineage("scenes", v.get("sha256")),
                    on_fail=failures.for_artifact("scenes"),
                )
            with stats.lock:
                stats.processed += done
                stats.failed += failed

        elif repair_type == "scene-vision":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")

            if not _vision_ready():
                continue
            scene_vision_settings = producers.with_model("scene_vision", vision.model)
            scene_vision_provider = vision.provider(settings=scene_vision_settings)

            assets = _filter(_page_missing(client, library_id, missing_scene_vision=True))
            if not assets:
                console.print("No assets found (already repaired?).")
                continue
            assets = _due("scene-vision", assets)
            # Representative frames come from the analysis proxy.
            assets = _waiting_for_proxy(assets)
            if not assets:
                continue

            videos = [{"asset_id": a["asset_id"], "rel_path": a["rel_path"], "sha256": a.get("sha256")}
                      for a in assets]

            from src.client.cli.video_index import run_video_enrich
            progress = _make_progress(console)
            with progress:
                tid = progress.add_task("Scene vision", total=len(videos), ok=0, fail=0)
                # As many scenes at once as the online machines take together (Settings → AI).
                done, failed = run_video_enrich(
                    concurrency=vision.capacity(),
                    client=client,
                    source_for=lambda v: analysis_cache.get(v["asset_id"]),
                    videos=_until(lambda: stop() or vision.down, videos, _taking("scene-vision")),
                    vision_provider=scene_vision_provider,
                    vision_model_id=vision.model,
                    console=console,
                    progress=progress,
                    task_id=tid,
                    lineage_for=lambda v: producers.lineage("scene_vision", v.get("sha256"),
                                                            used=scene_vision_settings),
                    on_fail=vision.on_fail("scene_vision"),
                )
            with stats.lock:
                stats.processed += done
                stats.failed += failed

        elif repair_type == "search-sync":
            console.print(f"\n[bold]Repairing: {desc} ({count})[/bold]")
            qs = "?force=true" if force else ""
            resp = client.post(f"/v1/upkeep/search-sync{qs}")
            result = resp.json()
            synced = result.get("synced", 0)
            sync_failed = result.get("failed", 0)
            console.print(f"  Search sync: {synced} synced, {sync_failed} failed")

    if render_thread is not None:
        render_thread.join()  # it stops between items too, when told to
    failures.flush()
    # Proxy cache is persistent (shared between scan and enrich) — do not clean up.

    console.print(f"\n[green bold]Repair complete.[/green bold] "
                  f"{stats.processed} fixed, {stats.failed} failed, {stats.skipped} skipped")
