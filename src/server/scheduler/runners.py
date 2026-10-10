"""Running one job of each kind (ADR-016 phase 4).

Each runner does what the worker's step did for one clip (faces: one
batch), with the same code: src/client/cli/repair.py's per-clip functions,
src/processing/video_index.py for scenes. Results are saved through the
API's batch routes on the brain (one clip each: the batch routes leave
search to the upkeep sweep instead of committing the index per clip), with
lineage, as before. A clip that can't be made is reported as failing (the
server tries it again after 5 minutes, doubling up to a day); trouble with
the AI machines, or the GPU running out of memory, charges no clip.

A runner returns NOT_TRIED when it couldn't try at all (the storage went
away, the scheduler is stopping): its clips aren't held back afterwards.
Once the scheduler is stopping, nothing more is saved: probe, render and
scenes save through _Saves (Stopped); the others look before each save.
(Scene descriptions finish the scene under way.)

Saving: every runner lets an error saving a clip escape, and the service
judges it by whose() it is (one job is one clip, but faces): the clip's
(the API refused what was made) is charged; trouble reaching the API or its
database charges nothing; anything else is a crash, charged once the clip
has crashed CRASHES_BEFORE_CHARGE times in a row (repository/lineage.py).
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


# A save the API refused for what's in it: the clip's failure, charged now.
REFUSED = frozenset({400, 413, 422})
# Not the clip's: who asked, the API or its database busy or away. Nothing
# is charged. (A 404 or 409 that keeps coming is counted, as a crash.)
NOT_THE_CLIPS = frozenset({401, 403, 408, 429, 502, 503, 504})


class Stopped(Exception):
    """The scheduler is stopping: nothing more is saved, and the job counts as not tried."""


class Crashed(Exception):
    """Some of a job's clips crashed (their face detection process died, say):
    they're counted towards a charge; waiting aren't."""

    def __init__(self, asset_ids: list[str], error: object, waiting: list[str] | None = None) -> None:
        super().__init__(str(error))
        self.asset_ids = list(asset_ids)
        self.error = error
        self.waiting = list(waiting or [])


def whose(error: object) -> str:
    """Whose an error that escaped a runner is: "clip" (charged now),
    "transient" (the API or its database: nothing charged) or "crash"
    (nothing charged, but counted: see the module's docstring)."""
    import httpx

    status = getattr(error, "status_code", None)
    if status in REFUSED:
        return "clip"
    if status in NOT_THE_CLIPS or isinstance(error, httpx.TransportError):
        return "transient"
    return "crash"


def _api_error(error: object) -> bool:
    import httpx

    from src.processing.api import LumiverbAPIError

    return isinstance(error, (LumiverbAPIError, httpx.HTTPError))


def _saving(fail: Callable[[str, object], Any]) -> Callable[[str, object], Any]:
    """on_fail for a step that saves itself (probe, render, scenes): its
    errors saving go to the service like every runner's (raised from here,
    so they escape the step) unless they're the clip's; the step's own
    failures (ffprobe, the render, the frames) are the clip's; this
    machine's (OSError: a full disk, the cache) go to the service too."""
    def on_fail(asset_id: str, error: object) -> Any:
        if (isinstance(error, (Stopped, OSError))
                or (_api_error(error) and whose(error) != "clip")):
            raise error  # type: ignore[misc]
        return fail(asset_id, error)
    return on_fail


class _Saves:
    """The account's client for a step that saves itself: once the scheduler
    is stopping, a save raises Stopped instead."""

    def __init__(self, acct: Account) -> None:
        self._acct = acct
        self._client = acct.client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def _write(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._acct.stopping.is_set():
            raise Stopped(path)
        return getattr(self._client, method)(path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self._write("post", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self._write("put", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        return self._write("patch", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self._write("delete", path, **kwargs)


def _out_of_memory(error: object) -> bool:
    """The GPU's trouble, not the clip's: torch's and ONNX Runtime's words for it."""
    text = str(error).lower()
    return "out of memory" in text or "failed to allocate" in text


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
            if acct.storage_gone(a["library_id"]):
                logger.info("scheduler: %s's storage went away; its storage work waits", a["rel_path"])
                acct.unreachable(a["library_id"])
                return NOT_TRIED
            waiting.append(a["asset_id"])
    return waiting or None


def probe(acct: Account, job: Job) -> Outcome:
    from src.client.cli.repair import _probe_one

    fail = _saving(acct.failures.for_artifact("probe"))
    client = _Saves(acct)
    return _on_storage(acct, job, lambda root, a: _probe_one(client, root, a, acct.producers, fail=fail))


def render(acct: Account, job: Job) -> Outcome:
    from src.client.cli.repair import _render_one
    from src.processing.video.analysis_proxy import AnalysisProxySettings

    here = acct.machine
    settings = AnalysisProxySettings.for_producer(acct.producers.settings("analysis_proxy"),
                                                  here.analysis_proxy_encoder, here.analysis_proxy_decoder,
                                                  here.gpu_decodes)
    fail = _saving(acct.failures.for_artifact("analysis_proxy"))
    client = _Saves(acct)
    return _on_storage(acct, job, lambda root, a: _render_one(client, root, a, settings, acct.analysis_cache,
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
    from src.processing.ingest import _backfill_one

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
    from src.processing.workers.captions.base import CaptionError

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
    from src.processing.workers.transcripts.base import TranscriptError

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
    """A video's scenes. A clip neither made nor reported waits the long while."""
    from src.processing.video_index import run_video_index

    if acct.stopping.is_set():
        return NOT_TRIED
    reported: set[str] = set()
    # One read of the settings for the job: what's found and what its lineage says agree.
    used = acct.producers.settings("scenes")
    videos = [{"asset_id": a["asset_id"], "rel_path": a["rel_path"], "duration_sec": a.get("duration_sec"),
               "sha256": a.get("sha256"), "redo": bool(a.get("redo"))} for a in job.items if a.get("duration_sec")]
    run_video_index(client=_Saves(acct), source_for=lambda v: acct.analysis_cache.get(v["asset_id"]),
                    videos=videos, console=_QUIET, progress=_Progress(), task_id=None,
                    lineage_for=lambda v: acct.producers.lineage("scenes", v.get("sha256"), used=used),
                    on_fail=_saving(_noting(acct.failures.for_artifact("scenes"), reported)), settings=used,
                    on_done=reported.add, stopping=acct.stopping)
    if acct.stopping.is_set():
        return NOT_TRIED
    return [i for i in job.asset_ids if i not in reported] or None


def scene_vision(acct: Account, job: Job) -> Outcome:
    """A video's scenes described (as scenes(): a clip neither made nor reported waits)."""
    from src.processing.video_index import run_video_enrich

    if acct.stopping.is_set():
        return NOT_TRIED
    model = acct.vision.model
    used = acct.producers.with_model("scene_vision", model)
    scene_provider = acct.vision.provider(settings=used)
    reported: set[str] = set()
    videos = [{"asset_id": a["asset_id"], "rel_path": a["rel_path"], "sha256": a.get("sha256"),
               "redo": bool(a.get("redo"))} for a in job.items]
    # One scene at a time: the job holds one of the vision machines' slots.
    run_video_enrich(concurrency=1, client=acct.client,
                     source_for=lambda v: acct.analysis_cache.get(v["asset_id"]), videos=videos,
                     vision_provider=scene_provider, vision_model_id=model, console=_QUIET,
                     progress=_Progress(), task_id=None,
                     lineage_for=lambda v: acct.producers.lineage("scene_vision", v.get("sha256"), used=used),
                     on_fail=_saving(_noting(acct.vision.on_fail("scene_vision"), reported)), on_done=reported.add)
    return [i for i in job.asset_ids if i not in reported] or None


def faces(acct: Account, job: Job) -> Outcome:
    if acct.stopping.is_set():
        return NOT_TRIED
    return acct.faces().run(acct, job)


_DIED = object()  # a face batch's process died or hung
_LET_GO = object()  # the face process was let go of (a stop, the account gone)


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
                                            maxtasksperchild=self._cfg.face_batches_per_process)

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
        from src.client.cli.repair import _generate_proxy_for_item

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
        if acct.stopping.is_set():  # no new process for an account let go of meanwhile
            return NOT_TRIED
        used = acct.producers.settings("faces")  # one read: what's found and its lineage agree
        lineage = acct.producers.lineage("faces", None, used=used)
        result = self._batch(ready, cache_path, lineage, used)
        if result is _LET_GO:
            return NOT_TRIED
        results, crashed = [], []
        if result is not _DIED:
            results.append(result)
        elif len(ready) == 1:
            crashed.append(ready[0]["asset_id"])
        else:
            # One bad photo mustn't hold the rest back: each is tried alone,
            # and only those whose process dies again count as crashed. Two
            # in a row dying is the machine's trouble (a hung GPU): the rest wait.
            logger.warning("scheduler: trying the %d clips of a face batch whose process died one at a time", len(ready))
            stopped = False
            for n, item in enumerate(ready):
                if acct.stopping.is_set():
                    stopped = True
                    break
                one = self._batch([item], cache_path, lineage, used)
                if one is _LET_GO:
                    stopped = True
                    break
                if one is not _DIED:
                    results.append(one)
                    continue
                crashed.append(item["asset_id"])
                if len(crashed) >= 2 and crashed[-2] == ready[n - 1]["asset_id"]:
                    logger.warning("scheduler: face detection keeps dying; the rest of the batch waits")
                    waiting.extend(i["asset_id"] for i in ready[n + 1:])
                    break
            if stopped:  # what was found meanwhile is still reported
                self._report(acct, results, waiting)
                return NOT_TRIED
        self._report(acct, results, waiting)
        if crashed:
            # The machine's trouble or the photo's: nothing is charged now, but
            # each clip's crashes are counted (the service, CRASHES_BEFORE_CHARGE).
            raise Crashed(crashed, "face detection's process died or hung", waiting=waiting)
        return waiting or None

    @staticmethod
    def _report(acct: Account, results: list[dict], waiting: list[str]) -> None:
        """Each batch's clips that failed: charged, but the GPU running out of memory (they wait)."""
        for r in results:
            for err in r.get("errors", []):
                if not err.get("asset_id"):
                    continue
                if _out_of_memory(err.get("error", "")):
                    waiting.append(err["asset_id"])
                else:
                    acct.failures.add("faces", err["asset_id"], err["error"])

    def _batch(self, items: list[dict], cache_path: str | None, lineage: dict, used: dict) -> Any:
        """One batch in the process: its result; _DIED when the process died
        or hung (it's replaced); _LET_GO when it was let go of meanwhile."""
        from src.client.cli.repair import _face_batch_worker

        with self._lock:
            if self._pool is None:
                self._pool = self._pool_factory()
            pool = self._pool
        try:
            pending = pool.apply_async(_face_batch_worker, (self._client.base_url, self._client.token, items,
                                                            cache_path, lineage, used))
            # A process killed mid-batch (out of memory, a segfault) never
            # answers: give up on the batch rather than wait forever.
            deadline = self._clock() + FACE_BATCH_TIMEOUT_SEC
            while True:
                try:
                    return pending.get(timeout=self._poll_sec)
                except mp.TimeoutError:
                    if self._pool is not pool:  # let go of (a stop, the account gone)
                        return _LET_GO
                    if self._clock() >= deadline:
                        raise
        except Exception:  # noqa: BLE001
            if self._pool is not pool:  # let go of before it could start
                return _LET_GO
            logger.exception("scheduler: face detection's process died or hung (%d clips); a new one takes over",
                             len(items))
            self._close(pool)
            return _DIED

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
    acct.set_reachable(scan_pass(acct.client, libraries, acct.scan_state, now=now, on_roots=acct.set_reachable))


def runners() -> dict[str, Callable[[Account, Job], Outcome]]:
    """The function each producer the scheduler runs names (its ``run``),
    loaded when first asked for: a producer's run module may import this one."""
    from src.producers import load
    from src.shared.producers import PRODUCERS

    return {p.kind: load(p.run) for p in PRODUCERS.values() if p.scheduled}
