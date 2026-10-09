"""Running one job of each kind (ADR-016 phase 4).

Each runner does what the worker's step did for one clip (faces: one
batch), with the same code: src/client/cli/repair.py's per-clip functions,
src/client/cli/video_index.py for scenes. Results are saved through the
API's batch routes on the brain (one clip each: the batch routes leave
search to the upkeep sweep instead of committing the index per clip), with
lineage, as before. A clip that can't be made is reported as failing (the
server tries it again after 5 minutes, doubling up to a day); trouble with
the AI machines, or the GPU running out of memory, charges no clip.

A runner returns NOT_TRIED when it couldn't try at all (the storage went
away, the scheduler is stopping): its clips aren't held back afterwards.
Once the scheduler is stopping, nothing more is saved.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from rich.console import Console

from src.server.scheduler.dispatch import Job, Outcome

if TYPE_CHECKING:
    from src.server.scheduler.account import Account

logger = logging.getLogger(__name__)

_QUIET = Console(quiet=True)

NOT_TRIED = "not_tried"
# A batch of face detection that takes longer than this is given up (the
# process is replaced; no clip is charged).
FACE_BATCH_TIMEOUT_SEC = 900.0
# How often a face batch looks up from its wait (its process let go of: a
# stop, the account gone).
FACE_POLL_SEC = 1.0


def _out_of_memory(error: object) -> bool:
    """The GPU's trouble, not the clip's: torch's and ONNX Runtime's words for it."""
    text = str(error).lower()
    return "out of memory" in text or "failed to allocate" in text


def _storage_gone(root: Any) -> bool:
    """The library's storage went away since the last look (an unmounted
    share is an empty folder)."""
    try:
        return next(iter(root.iterdir()), None) is None
    except OSError:
        return True


class _Progress:
    """What the step functions report progress to: nothing to show here."""

    console = _QUIET

    def advance(self, *args: Any, **kwargs: Any) -> None:
        pass

    def update(self, *args: Any, **kwargs: Any) -> None:
        pass


def _root_or_not_tried(acct: Account, a: dict) -> Any:
    root = acct.root(a["library_id"])
    if root is None:
        logger.info("scheduler: %s's storage isn't reachable; its storage work waits", a["rel_path"])
        acct.unreachable(a["library_id"])
    return root


def _on_storage(acct: Account, job: Job, step: Callable[[Any, dict], str]) -> Outcome:
    """A step on each clip's original. Its file missing, with the storage
    there, waits for the scan that says so; with the storage gone since the
    last look, nothing more is tried until the next look."""
    waiting: list[str] = []
    for a in job.items:
        if acct.stopping.is_set() or (root := _root_or_not_tried(acct, a)) is None:
            return NOT_TRIED
        if step(root, a) == "missing":
            if _storage_gone(root):
                logger.info("scheduler: %s's storage went away; its storage work waits", a["rel_path"])
                acct.unreachable(a["library_id"])
                return NOT_TRIED
            waiting.append(a["asset_id"])
    return waiting or None


def probe(acct: Account, job: Job) -> Outcome:
    from src.client.cli.repair import _probe_one

    fail = acct.failures.for_artifact("probe")
    return _on_storage(acct, job, lambda root, a: _probe_one(acct.client, root, a, acct.producers, fail=fail))


def render(acct: Account, job: Job) -> Outcome:
    from src.client.cli.repair import _render_one
    from src.client.video.analysis_proxy import AnalysisProxySettings

    cfg = acct.cfg
    settings = AnalysisProxySettings.for_producer(acct.producers.settings("analysis_proxy"),
                                                  cfg.analysis_proxy_encoder, cfg.analysis_proxy_decoder,
                                                  cfg.gpu_decodes)
    fail = acct.failures.for_artifact("analysis_proxy")
    return _on_storage(acct, job, lambda root, a: _render_one(acct.client, root, a, settings, acct.analysis_cache,
                                                              acct.producers, fail=fail))


def clip(acct: Account, job: Job) -> Outcome:
    from src.client.cli.repair import _repair_embed_one

    provider, used = acct.clip()
    waiting: list[str] = []
    for a in job.items:
        sha = a.get("sha256")
        try:
            item = _repair_embed_one(asset_id=a["asset_id"], rel_path=a["rel_path"], clip_provider=provider,
                                     proxy_cache=acct.proxy_cache(a["library_id"]))
            error: object = "no embedding (no proxy)"
        except Exception as e:  # noqa: BLE001 — the clip's, reported as failing (unless it's the GPU's)
            if _out_of_memory(e):
                logger.warning("scheduler: the GPU ran out of memory for CLIP; %s waits", a["rel_path"])
                waiting.append(a["asset_id"])
                continue
            item, error = None, e
        if item is None:
            acct.failures.add("clip", a["asset_id"], error)
            continue
        if acct.stopping.is_set():
            return NOT_TRIED
        acct.client.post("/v1/assets/batch-embeddings", json={
            "items": [{**item, "source_sha256": sha}], "lineage": acct.producers.lineage("clip", None, used=used)})
    return waiting or None


def vision(acct: Account, job: Job) -> Outcome:
    from src.client.cli.ingest import _backfill_one

    provider, model = acct.vision_provider()
    used = acct.producers.with_model("vision", model)
    fail = acct.vision.on_fail("vision")
    waiting: list[str] = []
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
            if not fail(a["asset_id"], error):  # the machines' trouble: uncharged
                waiting.append(a["asset_id"])
            continue
        if acct.stopping.is_set():
            return NOT_TRIED
        acct.client.post("/v1/assets/batch-vision", json={
            "items": [{**result, "source_sha256": sha}], "lineage": acct.producers.lineage("vision", None, used=used)})
    return waiting or None


def ocr(acct: Account, job: Job) -> Outcome:
    from src.client.cli.repair import _ocr_one
    from src.client.workers.captions.base import CaptionError

    provider, model = acct.vision_provider()
    used = acct.producers.with_model("ocr", model)
    fail = acct.vision.on_fail("ocr")
    waiting: list[str] = []
    for a in job.items:
        sha = a.get("sha256")
        try:
            result = _ocr_one(asset_id=a["asset_id"], rel_path=a["rel_path"], ocr_provider=provider,
                              proxy_cache=acct.proxy_cache(a["library_id"]))
            error: object = "no OCR result (no proxy, or the image couldn't be read: see the log)"
        except CaptionError as e:
            result, error = None, e
        if result is None:
            if not fail(a["asset_id"], error):
                waiting.append(a["asset_id"])
            continue
        if acct.stopping.is_set():
            return NOT_TRIED
        acct.client.post("/v1/assets/batch-ocr", json={
            "items": [{"asset_id": a["asset_id"], "ocr_text": result["ocr_text"], "source_sha256": sha}],
            "model_id": model, "lineage": acct.producers.lineage("ocr", None, used=used)})
    return waiting or None


def transcript(acct: Account, job: Job) -> Outcome:
    from src.client.cli.repair import _transcribe_one
    from src.client.workers.transcripts.base import TranscriptError

    vad_ms = acct.producers.settings("transcript")["vad_min_silence_ms"]
    used = acct.producers.with_model("transcript", acct.transcripts.model)
    transcriber = acct.transcriber()
    fail = acct.transcripts.on_fail("transcript")
    waiting: list[str] = []
    for a in job.items:
        asset_id = a["asset_id"]
        source = acct.analysis_cache.get(asset_id)
        if source is None:
            acct.failures.add("transcript", asset_id, "its analysis proxy couldn't be read")
            continue
        try:
            result = _transcribe_one(source, transcriber, vad_ms)
        except TranscriptError as e:
            # The machines' trouble stops transcription, charging no clip;
            # the clip's is charged once a check finds the machine fine.
            if not fail(asset_id, e):
                waiting.append(asset_id)
            continue
        except Exception:  # noqa: BLE001 — one clip's surprise
            logger.exception("scheduler: transcribing %s failed", a["rel_path"])
            result = None
        if result is None:
            acct.failures.add("transcript", asset_id, "transcription failed (see the log)")
            continue
        if acct.stopping.is_set():
            return NOT_TRIED
        srt_text, language = result
        acct.client.post(f"/v1/assets/{asset_id}/transcript", json={
            "srt": srt_text, "language": language, "source": "whisper",
            "lineage": acct.producers.lineage("transcript", a.get("sha256"), used=used)})
    return waiting or None


def _noting(fail: Callable[[str, object], Any], reported: set[str]) -> Callable[[str, object], None]:
    """on_fail that remembers the clips it reported (a guard's False: not charged)."""
    def on_fail(asset_id: str, error: object) -> None:
        if fail(asset_id, error) is not False:
            reported.add(asset_id)
    return on_fail


def scenes(acct: Account, job: Job) -> Outcome:
    """A video's scenes. What's made isn't seen here (the server stops
    listing it), so every clip not reported waits the long while."""
    from src.client.cli.video_index import run_video_index

    if acct.stopping.is_set():
        return NOT_TRIED
    reported: set[str] = set()
    videos = [{"asset_id": a["asset_id"], "rel_path": a["rel_path"], "duration_sec": a.get("duration_sec"),
               "sha256": a.get("sha256")} for a in job.items if a.get("duration_sec")]
    run_video_index(client=acct.client, source_for=lambda v: acct.analysis_cache.get(v["asset_id"]),
                    videos=videos, console=_QUIET, progress=_Progress(), task_id=None,
                    lineage_for=lambda v: acct.producers.lineage("scenes", v.get("sha256")),
                    on_fail=_noting(acct.failures.for_artifact("scenes"), reported))
    return [i for i in job.asset_ids if i not in reported] or None


def scene_vision(acct: Account, job: Job) -> Outcome:
    """A video's scenes described (as scenes(): what's made isn't seen here)."""
    from src.client.cli.video_index import run_video_enrich

    if acct.stopping.is_set():
        return NOT_TRIED
    model = acct.vision.model
    used = acct.producers.with_model("scene_vision", model)
    scene_provider = acct.vision.provider(settings=used)
    reported: set[str] = set()
    videos = [{"asset_id": a["asset_id"], "rel_path": a["rel_path"], "sha256": a.get("sha256"),
               "upgrade": bool(a.get("upgrade"))} for a in job.items]
    # One scene at a time: the job holds one of the vision machines' slots.
    run_video_enrich(concurrency=1, client=acct.client,
                     source_for=lambda v: acct.analysis_cache.get(v["asset_id"]), videos=videos,
                     vision_provider=scene_provider, vision_model_id=model, console=_QUIET,
                     progress=_Progress(), task_id=None,
                     lineage_for=lambda v: acct.producers.lineage("scene_vision", v.get("sha256"), used=used),
                     on_fail=_noting(acct.vision.on_fail("scene_vision"), reported))
    return [i for i in job.asset_ids if i not in reported] or None


def faces(acct: Account, job: Job) -> Outcome:
    if acct.stopping.is_set():
        return NOT_TRIED
    return acct.faces().run(acct, job)


class FaceRunner:
    """Face detection in a subprocess, kept between jobs so the model loads
    once per so many batches (ONNX Runtime leaks: a process that has done
    its share is replaced)."""

    def __init__(self, client: Any, cfg: Any,
                 pool_factory: Callable[[], Any] | None = None, *,
                 clock: Callable[[], float] = time.monotonic, poll_sec: float = FACE_POLL_SEC) -> None:
        self._client = client
        self._cfg = cfg
        self._pool: Any = None
        self._pool_factory = pool_factory or self._new_pool
        self._clock = clock
        self._poll_sec = poll_sec
        self._running = 0
        self._lock = threading.Lock()

    def _new_pool(self) -> Any:
        from src.client.cli.repair import _silence_subprocess_stdout

        return mp.get_context("spawn").Pool(1, initializer=_silence_subprocess_stdout,
                                            maxtasksperchild=self._cfg.face_batch_limit)

    @property
    def idle(self) -> bool:
        return self._running == 0

    def run(self, acct: Account, job: Job) -> Outcome:
        with self._lock:
            self._running += 1
        try:
            return self._run(acct, job)
        finally:
            with self._lock:
                self._running -= 1

    def _run(self, acct: Account, job: Job) -> Outcome:
        from src.client.cli.repair import _face_batch_worker, _generate_proxy_for_item

        ready, waiting, cache_path = [], [], None
        for a in job.items:
            cache = acct.proxy_cache(a["library_id"])
            cache_path = str(cache.path)
            item = _generate_proxy_for_item(a, acct.root(a["library_id"]), cache)
            if item is None:  # the file changed since it was hashed; its scan comes first
                waiting.append(a["asset_id"])
            else:
                ready.append(item)
        if not ready:
            return waiting or None
        with self._lock:
            if self._pool is None:
                self._pool = self._pool_factory()
            pool = self._pool
        try:
            pending = pool.apply_async(_face_batch_worker, (self._client.base_url, self._client.token, ready,
                                                            cache_path, acct.producers.lineage("faces", None)))
            # A process killed mid-batch (out of memory, a segfault) never
            # answers: give up on the batch rather than wait forever.
            deadline = self._clock() + FACE_BATCH_TIMEOUT_SEC
            while True:
                try:
                    result = pending.get(timeout=self._poll_sec)
                    break
                except mp.TimeoutError:
                    if self._pool is not pool:  # let go of (a stop, the account gone)
                        return NOT_TRIED
                    if self._clock() >= deadline:
                        raise
        except Exception:  # noqa: BLE001
            # The batch's process died or hung (the GPU, ONNX, memory): this
            # machine's problem, not the clips', so none is charged; they're
            # tried again later, by a new process.
            logger.exception("scheduler: face detection's process died or hung; a new one takes the next batch")
            self._close(pool)
            return list(job.asset_ids)
        for err in result.get("errors", []):
            if not err.get("asset_id"):
                continue
            if _out_of_memory(err.get("error", "")):
                waiting.append(err["asset_id"])
            else:
                acct.failures.add("faces", err["asset_id"], err["error"])
        return waiting or None

    def close(self) -> bool:
        """Let go of the process (a batch under way ends uncharged). True when there was one."""
        return self._close(None)

    def _close(self, only: Any) -> bool:
        with self._lock:
            pool = self._pool
            if pool is None or (only is not None and pool is not only):
                return False
            self._pool = None
        try:
            pool.terminate()
            pool.join()
        except Exception:  # noqa: BLE001
            pass
        return True


def scan(acct: Account, job: Job, *, now: float) -> None:
    """Look at the account's libraries: scan what's due, and note where each one's storage is."""
    from src.server.scheduler.scans import scan_pass

    libraries = acct.client.get("/v1/libraries").json()
    acct.set_libraries(libraries)
    acct.set_reachable(scan_pass(acct.client, libraries, acct.scan_state, now=now))


RUNNERS: dict[str, Callable[[Account, Job], Outcome]] = {
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
