"""Running one job of each kind (ADR-016 phase 4).

Each runner does what the worker's step did for one clip (faces: one
batch), with the same code: src/client/cli/repair.py's per-clip functions,
src/client/cli/video_index.py for scenes. Results are saved through the
API's routes on the brain, with lineage, as before. A clip that can't be
made is reported as failing (the server tries it again after 5 minutes,
doubling up to a day); trouble with the AI machines stops their jobs
without charging any clip.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from rich.console import Console

from src.server.scheduler.dispatch import Job

if TYPE_CHECKING:
    from src.server.scheduler.account import Account

logger = logging.getLogger(__name__)

_QUIET = Console(quiet=True)


class _Progress:
    """What the step functions report progress to: nothing to show here."""

    console = _QUIET

    def advance(self, *args: Any, **kwargs: Any) -> None:
        pass

    def update(self, *args: Any, **kwargs: Any) -> None:
        pass


def probe(acct: Account, job: Job) -> None:
    from src.client.cli.repair import _probe_one

    for a in job.items:
        root = acct.root(a["library_id"])
        if root is None:
            logger.info("scheduler: %s's storage went away; probing waits", a["rel_path"])
            return
        _probe_one(acct.client, root, a, acct.producers, fail=acct.failures.for_artifact("probe"))


def render(acct: Account, job: Job) -> None:
    from src.client.cli.repair import _render_one
    from src.client.video.analysis_proxy import AnalysisProxySettings

    cfg = acct.cfg
    settings = AnalysisProxySettings.for_producer(acct.producers.settings("analysis_proxy"),
                                                  cfg.analysis_proxy_encoder, cfg.analysis_proxy_decoder,
                                                  cfg.gpu_decodes)
    for a in job.items:
        root = acct.root(a["library_id"])
        if root is None:
            logger.info("scheduler: %s's storage went away; rendering waits", a["rel_path"])
            return
        _render_one(acct.client, root, a, settings, acct.analysis_cache, acct.producers,
                    fail=acct.failures.for_artifact("analysis_proxy"))


def clip(acct: Account, job: Job) -> None:
    from src.client.cli.repair import _repair_embed_one

    provider, used = acct.clip()
    for a in job.items:
        sha = a.get("sha256")
        try:
            item = _repair_embed_one(asset_id=a["asset_id"], rel_path=a["rel_path"], clip_provider=provider,
                                     proxy_cache=acct.proxy_cache(a["library_id"]))
            error: object = "no embedding (no proxy)"
        except Exception as e:  # noqa: BLE001 — the clip's, reported as failing
            item, error = None, e
        if item is None:
            acct.failures.add("clip", a["asset_id"], error)
            continue
        acct.client.post(f"/v1/assets/{a['asset_id']}/embeddings",
                         json={**item, "source_sha256": sha, "lineage": acct.producers.lineage("clip", sha, used=used)})


def vision(acct: Account, job: Job) -> None:
    from src.client.cli.ingest import _backfill_one

    provider, model = acct.vision_provider()
    used = acct.producers.with_model("vision", model)
    fail = acct.vision.on_fail("vision")
    for a in job.items:
        sha = a.get("sha256")
        try:
            result = _backfill_one(asset_id=a["asset_id"], rel_path=a["rel_path"], vision_model_id=model,
                                   vision_provider=provider, proxy_cache=acct.proxy_cache(a["library_id"]),
                                   client=acct.client)
            error: object = "no description (no proxy, or the model returned nothing)"
        except Exception as e:  # noqa: BLE001 — the guard decides whose fault it is
            result, error = None, e
        if result is None:
            fail(a["asset_id"], error)
            continue
        acct.client.post(f"/v1/assets/{a['asset_id']}/vision", json={
            **result, "source_sha256": sha, "lineage": acct.producers.lineage("vision", sha, used=used)})


def ocr(acct: Account, job: Job) -> None:
    from src.client.cli.repair import _ocr_one
    from src.client.workers.captions.base import CaptionError

    provider, model = acct.vision_provider()
    used = acct.producers.with_model("ocr", model)
    fail = acct.vision.on_fail("ocr")
    for a in job.items:
        sha = a.get("sha256")
        try:
            result = _ocr_one(asset_id=a["asset_id"], rel_path=a["rel_path"], ocr_provider=provider,
                              proxy_cache=acct.proxy_cache(a["library_id"]))
            error: object = "no OCR result (no proxy, or the image couldn't be read: see the log)"
        except CaptionError as e:
            result, error = None, e
        if result is None:
            fail(a["asset_id"], error)
            continue
        acct.client.post(f"/v1/assets/{a['asset_id']}/ocr", json={
            "ocr_text": result["ocr_text"], "model_id": model,
            "lineage": acct.producers.lineage("ocr", sha, used=used)})


def transcript(acct: Account, job: Job) -> None:
    from src.client.cli.repair import _transcribe_one
    from src.client.workers.transcripts.base import TranscriptError

    vad_ms = acct.producers.settings("transcript")["vad_min_silence_ms"]
    used = acct.producers.with_model("transcript", acct.transcripts.model)
    transcriber = acct.transcriber()
    fail = acct.transcripts.on_fail("transcript")
    for a in job.items:
        asset_id = a["asset_id"]
        source = acct.analysis_cache.get(asset_id)
        if source is None:
            logger.warning("scheduler: no analysis proxy for %s: %s", asset_id, a["rel_path"])
            continue
        try:
            result = _transcribe_one(source, transcriber, vad_ms)
        except TranscriptError as e:
            # The machines' trouble stops transcription, charging no clip;
            # the clip's is charged once a check finds the machine fine.
            fail(asset_id, e)
            continue
        except Exception:  # noqa: BLE001 — one clip's surprise
            logger.exception("scheduler: transcribing %s failed", a["rel_path"])
            result = None
        if result is None:
            acct.failures.add("transcript", asset_id, "transcription failed (see the log)")
            continue
        srt_text, language = result
        acct.client.post(f"/v1/assets/{asset_id}/transcript", json={
            "srt": srt_text, "language": language, "source": "whisper",
            "lineage": acct.producers.lineage("transcript", a.get("sha256"), used=used)})


def scenes(acct: Account, job: Job) -> None:
    from src.client.cli.video_index import run_video_index

    videos = [{"asset_id": a["asset_id"], "rel_path": a["rel_path"], "duration_sec": a.get("duration_sec"),
               "sha256": a.get("sha256")} for a in job.items if a.get("duration_sec")]
    run_video_index(client=acct.client, source_for=lambda v: acct.analysis_cache.get(v["asset_id"]),
                    videos=videos, console=_QUIET, progress=_Progress(), task_id=None,
                    lineage_for=lambda v: acct.producers.lineage("scenes", v.get("sha256")),
                    on_fail=acct.failures.for_artifact("scenes"))


def scene_vision(acct: Account, job: Job) -> None:
    from src.client.cli.video_index import run_video_enrich

    model = acct.vision.model
    used = acct.producers.with_model("scene_vision", model)
    scene_provider = acct.vision.provider(settings=used)
    videos = [{"asset_id": a["asset_id"], "rel_path": a["rel_path"], "sha256": a.get("sha256"),
               "upgrade": bool(a.get("upgrade"))} for a in job.items]
    # One scene at a time: the job holds one of the vision machines' slots.
    run_video_enrich(concurrency=1, client=acct.client,
                     source_for=lambda v: acct.analysis_cache.get(v["asset_id"]), videos=videos,
                     vision_provider=scene_provider, vision_model_id=model, console=_QUIET,
                     progress=_Progress(), task_id=None,
                     lineage_for=lambda v: acct.producers.lineage("scene_vision", v.get("sha256"), used=used),
                     on_fail=acct.vision.on_fail("scene_vision"))


def faces(acct: Account, job: Job) -> None:
    acct.faces().run(acct, job)


class FaceRunner:
    """Face detection in a subprocess, kept between jobs so the model loads
    once per so many batches (ONNX Runtime leaks: a process that has done
    its share is replaced)."""

    def __init__(self, client: Any, cfg: Any,
                 pool_factory: Callable[[], Any] | None = None) -> None:
        self._client = client
        self._cfg = cfg
        self._pool: Any = None
        self._pool_factory = pool_factory or self._new_pool

    def _new_pool(self) -> Any:
        from src.client.cli.repair import _silence_subprocess_stdout

        return mp.get_context("spawn").Pool(1, initializer=_silence_subprocess_stdout,
                                            maxtasksperchild=self._cfg.face_batch_limit)

    def run(self, acct: Account, job: Job) -> None:
        from src.client.cli.repair import _face_batch_worker, _generate_proxy_for_item

        ready, cache_path = [], None
        for a in job.items:
            cache = acct.proxy_cache(a["library_id"])
            cache_path = str(cache.path)
            item = _generate_proxy_for_item(a, acct.root(a["library_id"]), cache)
            if item is not None:  # None: the file changed since it was hashed; its scan comes first
                ready.append(item)
        if not ready:
            return
        if self._pool is None:
            self._pool = self._pool_factory()
        try:
            result = self._pool.apply(_face_batch_worker, (self._client.base_url, self._client.token, ready,
                                                           cache_path, acct.producers.lineage("faces", None)))
        except Exception:  # noqa: BLE001
            # The batch's process died (the GPU, ONNX, memory): this machine's
            # problem, not the clips', so none is charged; they're tried again later.
            logger.exception("scheduler: face detection's process died; a new one takes the next batch")
            self.close()
            return
        for err in result.get("errors", []):
            if err.get("asset_id"):
                acct.failures.add("faces", err["asset_id"], err["error"])

    def close(self) -> None:
        if self._pool is not None:
            pool, self._pool = self._pool, None
            try:
                pool.terminate()
                pool.join()
            except Exception:  # noqa: BLE001
                pass


def scan(acct: Account, job: Job, *, now: float) -> None:
    """Look at the account's libraries: scan what's due, and note which can be reached."""
    from src.server.scheduler.scans import scan_pass

    libraries = acct.client.get("/v1/libraries").json()
    acct.set_libraries(libraries)
    acct.reachable = scan_pass(acct.client, libraries, acct.scan_state, now=now)


RUNNERS: dict[str, Callable[[Account, Job], None]] = {
    "probe": probe,
    "render": render,
    "clip": clip,
    "vision": vision,
    "ocr": ocr,
    "faces": faces,
    "transcript": transcript,
    "scenes": scenes,
    "scene_vision": scene_vision,
}
