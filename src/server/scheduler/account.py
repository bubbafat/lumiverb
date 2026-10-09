"""What one account's jobs run with (ADR-016 phase 4).

Its API client (the scheduler saves results through the API's own routes,
as the worker did: the same checks and lineage), the server's producer
settings, its AI machines (Settings → AI), the caches of proxies and
analysis copies, and the models loaded once (CLIP, face detection), let go
of after a while unused so the GPU is free for the rest.

Settings and machines are read again every minute, off the scheduler's
dispatching thread; machines that can't be used are looked at again as
often. No job of the account's is handed out until its settings were read
once. Each library's storage is looked at by the scan pass only, and jobs
go by that look (several jobs probing one mount at once would see it as
unreachable).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.server.scheduler.scans import ScanState

logger = logging.getLogger(__name__)

# Settings and machines are read again this often (a new model reaches the
# scheduler within a minute)...
REFRESH_SEC = 60.0
# ...and sooner when the server didn't answer.
RETRY_SEC = 15.0
# A model unused this long is let go of (its GPU memory with it).
IDLE_SEC = 600.0


class Account:
    def __init__(self, tenant_id: str, client: Any, scan_state: ScanState, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        from src.client.cli.config import load_config
        from src.client.cli.failure_report import FailureReport
        from src.client.cli.producer_settings import ProducerSettings
        from src.client.cli.transcript_guard import TranscriptGuard
        from src.client.cli.vision_guard import VisionGuard
        from src.client.proxy.analysis_cache import AnalysisProxyCache

        self.tenant_id = tenant_id
        self.client = client
        self.scan_state = scan_state
        self.cfg = load_config()
        self._clock = clock
        self.failures = FailureReport(client)
        self.producers = ProducerSettings(client, fetch=False)  # read by refresh()
        self.vision = VisionGuard(client, self.failures)
        self.transcripts = TranscriptGuard(client, self.failures)
        self.analysis_cache = AnalysisProxyCache(client)
        # Set when the scheduler stops: jobs in hand save nothing more.
        self.stopping = threading.Event()
        # The server's settings were read at least once: its jobs can go.
        self.settings_ready = False
        # library_id -> the library; and its storage on this machine at the
        # last scan pass's look (None: not reachable).
        self.libraries: dict[str, dict] = {}
        self.roots: dict[str, Path | None] = {}
        self._lock = threading.Lock()
        self._proxy_caches: dict[str, Any] = {}
        self._clip: tuple[tuple, Any] | None = None
        self._clip_loading = threading.Lock()
        self._faces: Any = None
        self._vision_provider: tuple[tuple, Any] | None = None
        self._transcriber: tuple[str, Any] | None = None
        self._refreshed_at: float | None = None
        self._job_checked_at: dict[str, float] = {}
        self._used: dict[str, float] = {}
        self.ready = {"vision": False, "transcripts": False}

    # -- settings and machines ----------------------------------------------

    def refresh(self, force: bool = False) -> None:
        """Read the settings and libraries again when due, check the AI
        machines, and let go of idle models. Slow (it asks each machine): the
        scheduler calls it off its dispatching thread."""
        now = self._clock()
        wait = REFRESH_SEC if self.settings_ready else RETRY_SEC
        if force or self._refreshed_at is None or now - self._refreshed_at >= wait:
            self._refreshed_at = now
            if self.producers.refresh():
                self.settings_ready = True
            try:
                self.set_libraries(self.client.get("/v1/libraries").json())
            except Exception:  # noqa: BLE001 — tried again at the next refresh
                logger.exception("scheduler: couldn't read %s's libraries", self.tenant_id)
            self._job_checked_at.clear()
        for job, guard in (("vision", self.vision), ("transcripts", self.transcripts)):
            checked = self._job_checked_at.get(job)
            if checked is None or now - checked >= REFRESH_SEC:
                self._job_checked_at[job] = now
                try:
                    self.ready[job] = guard.check()
                except Exception:  # noqa: BLE001
                    logger.exception("scheduler: checking %s's %s machines failed", self.tenant_id, job)
                    self.ready[job] = False
            elif guard.down:
                self.ready[job] = False
        self._let_go_of_idle_models(now)

    def set_libraries(self, libraries: list[dict]) -> None:
        with self._lock:
            self.libraries = {lib["library_id"]: lib for lib in libraries}

    def set_reachable(self, roots: dict[str, Path | None]) -> None:
        """The scan pass's look at each library's storage."""
        with self._lock:
            self.roots = dict(roots)

    @property
    def reachable(self) -> dict[str, bool]:
        return {i: r is not None for i, r in self.roots.items()}

    def unreachable(self, library_id: str) -> None:
        """A job found the storage gone: storage work in it waits for the next look."""
        with self._lock:
            self.roots[library_id] = None

    def library_ids(self, *, storage: bool) -> list[str]:
        """Libraries a kind's jobs can run in: those whose storage was reachable at the last look, when it reads originals."""
        with self._lock:
            return [i for i in self.libraries if not storage or self.roots.get(i) is not None]

    def root(self, library_id: str) -> Path | None:
        """The library's storage on this machine at the last look, or None."""
        with self._lock:
            return self.roots.get(library_id)

    def capacity(self, job: str) -> int:
        """Requests the job's online machines take at once (0 while none can be used)."""
        guard = self.vision if job == "vision" else self.transcripts
        if not self.ready[job] or guard.down:
            return 0
        return guard.capacity()

    # -- caches and models --------------------------------------------------

    def proxy_cache(self, library_id: str) -> Any:
        """The images CLIP, faces and vision see, for clips of one library
        (made from the original while its storage is reachable)."""
        from src.client.cli.repair import PROXY_CACHE_EDGE
        from src.client.proxy.proxy_cache import ProxyCache

        root = self.root(library_id)
        with self._lock:
            cache = self._proxy_caches.get(library_id)
            if cache is None or cache._root_path != root:
                cache = ProxyCache(max_edge=PROXY_CACHE_EDGE, root_path=root, client=self.client)
                self._proxy_caches[library_id] = cache
            return cache

    def clip(self) -> tuple[Any, dict]:
        """The CLIP model, loaded once per settings (not while holding the
        account's lock: loading takes seconds), and the settings it's used with."""
        from src.client.cli.repair import PROXY_CACHE_EDGE
        from src.client.workers.embeddings.clip_provider import CLIPEmbeddingProvider

        settings = self.producers.settings("clip")
        key = (settings["model"], settings["pretrained"])
        with self._clip_loading:
            current = self._clip
            if current is None or current[0] != key:
                current = (key, CLIPEmbeddingProvider(model_name=settings["model"],
                                                      pretrained=settings["pretrained"]))
                self._clip = current
        self._used["clip"] = self._clock()
        # The images CLIP sees are the proxy cache's: its size is what was used.
        return current[1], {**settings, "input_edge": PROXY_CACHE_EDGE}

    def vision_provider(self) -> tuple[Any, str]:
        """Describes and reads images on whichever vision machine is free, and the model."""
        model = self.vision.model
        vision_settings = self.producers.with_model("vision", model)
        ocr_settings = self.producers.with_model("ocr", model)
        key = (model, repr(sorted(vision_settings.items())), repr(sorted(ocr_settings.items())))
        with self._lock:
            if self._vision_provider is None or self._vision_provider[0] != key:
                self._vision_provider = (key, self.vision.provider(settings=vision_settings,
                                                                   ocr_settings=ocr_settings))
            return self._vision_provider[1], model

    def transcriber(self) -> Any:
        model = self.transcripts.model
        with self._lock:
            if self._transcriber is None or self._transcriber[0] != model:
                self._transcriber = (model, self.transcripts.transcriber())
            return self._transcriber[1]

    def faces(self) -> Any:
        from src.server.scheduler.runners import FaceRunner

        with self._lock:
            if self._faces is None:
                self._faces = FaceRunner(self.client, self.cfg)
            self._used["faces"] = self._clock()
            return self._faces

    def _let_go_of_idle_models(self, now: float) -> None:
        if self._clip is not None and now - self._used.get("clip", now) >= IDLE_SEC:
            with self._clip_loading:
                self._clip = None
            _free_gpu_memory()
            logger.info("scheduler: let go of %s's CLIP model (unused)", self.tenant_id)
        faces = self._faces
        if faces is not None and now - self._used.get("faces", now) >= IDLE_SEC and faces.idle and faces.close():
            logger.info("scheduler: let go of %s's face detection process (unused)", self.tenant_id)

    def close(self) -> None:
        self.stopping.set()
        self.failures.flush()
        if self._faces is not None:
            self._faces.close()
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


def _free_gpu_memory() -> None:
    import gc

    gc.collect()  # the model's tensors, held by reference cycles, before the cache is emptied
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001 — no torch, or no GPU
        pass
