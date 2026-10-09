"""What one account's jobs run with (ADR-016 phase 4).

Its API client (the scheduler saves results through the API's own routes,
as the worker did: the same checks and lineage), the server's producer
settings, its AI machines (Settings → AI), the caches of proxies and
analysis copies, and the models loaded once (CLIP, face detection).
Settings and machines are read again every few minutes; machines that
can't be used are looked at again sooner.
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

# Settings and machines are read again this often...
REFRESH_SEC = 300.0
# ...and a job whose machines can't be used is looked at again this soon.
DOWN_RECHECK_SEC = 60.0


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
        self.producers = ProducerSettings(client)
        self.vision = VisionGuard(client, self.failures)
        self.transcripts = TranscriptGuard(client, self.failures)
        self.analysis_cache = AnalysisProxyCache(client)
        # library_id -> the library, and whether its storage is reachable (the last scan pass's look).
        self.libraries: dict[str, dict] = {}
        self.reachable: dict[str, bool] = {}
        self._lock = threading.Lock()
        self._proxy_caches: dict[str, Any] = {}
        self._clip: tuple[tuple, Any] | None = None
        self._faces: Any = None
        self._vision_provider: tuple[tuple, Any] | None = None
        self._transcriber: tuple[str, Any] | None = None
        self._refreshed_at: float | None = None
        self._job_checked_at: dict[str, float] = {}
        self.ready = {"vision": False, "transcripts": False}

    # -- settings and machines ----------------------------------------------

    def refresh(self, force: bool = False) -> None:
        """Read the settings and libraries again when due, and check the AI machines."""
        now = self._clock()
        if force or self._refreshed_at is None or now - self._refreshed_at >= REFRESH_SEC:
            self._refreshed_at = now
            try:
                self.producers.refresh()
                self.set_libraries(self.client.get("/v1/libraries").json())
            except Exception:  # noqa: BLE001 — tried again at the next refresh
                logger.exception("scheduler: couldn't read %s's settings", self.tenant_id)
            self._job_checked_at.clear()
        for job, guard in (("vision", self.vision), ("transcripts", self.transcripts)):
            checked = self._job_checked_at.get(job)
            wait = REFRESH_SEC if self.ready[job] and not guard.down else DOWN_RECHECK_SEC
            if checked is None or now - checked >= wait:
                self._job_checked_at[job] = now
                try:
                    self.ready[job] = guard.check()
                except Exception:  # noqa: BLE001
                    logger.exception("scheduler: checking %s's %s machines failed", self.tenant_id, job)
                    self.ready[job] = False
            elif guard.down:
                self.ready[job] = False

    def set_libraries(self, libraries: list[dict]) -> None:
        with self._lock:
            self.libraries = {lib["library_id"]: lib for lib in libraries}

    def library_ids(self, *, storage: bool) -> list[str]:
        """Libraries a kind's jobs can run in: those whose storage is reachable, when it reads the originals."""
        with self._lock:
            return [i for i in self.libraries if not storage or self.reachable.get(i)]

    def root(self, library_id: str) -> Path | None:
        """The library's storage on this machine, or None while it can't be reached."""
        from src.client.cli.roots import reachable_root

        library = self.libraries.get(library_id)
        return reachable_root(library) if library else None

    def capacity(self, job: str) -> int:
        """Requests the job's online machines take at once (0 while none can be used)."""
        guard = self.vision if job == "vision" else self.transcripts
        if not self.ready[job] or guard.down:
            return 0
        return guard.capacity()

    # -- caches and models --------------------------------------------------

    def proxy_cache(self, library_id: str) -> Any:
        """The images CLIP, faces and vision see, for clips of one library."""
        from src.client.cli.repair import PROXY_CACHE_EDGE
        from src.client.proxy.proxy_cache import ProxyCache

        with self._lock:
            cache = self._proxy_caches.get(library_id)
            if cache is None:
                cache = ProxyCache(max_edge=PROXY_CACHE_EDGE, root_path=self.root(library_id), client=self.client)
                self._proxy_caches[library_id] = cache
            return cache

    def clip(self) -> tuple[Any, dict]:
        """The CLIP model, loaded once per settings, and the settings it's used with."""
        from src.client.cli.repair import PROXY_CACHE_EDGE
        from src.client.workers.embeddings.clip_provider import CLIPEmbeddingProvider

        settings = self.producers.settings("clip")
        key = (settings["model"], settings["pretrained"])
        with self._lock:
            if self._clip is None or self._clip[0] != key:
                self._clip = (key, CLIPEmbeddingProvider(model_name=settings["model"],
                                                         pretrained=settings["pretrained"]))
            provider = self._clip[1]
        # The images CLIP sees are the proxy cache's: its size is what was used.
        return provider, {**settings, "input_edge": PROXY_CACHE_EDGE}

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
            return self._faces

    def close(self) -> None:
        self.failures.flush()
        if self._faces is not None:
            self._faces.close()
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass
