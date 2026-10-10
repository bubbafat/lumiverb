"""What one account's jobs run with (ADR-016 phase 4).

Its API client (the scheduler saves results through the API's own routes,
as the worker did: the same checks and lineage), the server's producer
settings, its AI machines (Settings → AI: a guard per AI job the producers
declare), the caches of proxies and analysis copies, and the scheduler's
models (CLIP, face detection: src/server/scheduler/models.py), which every
account shares.

Settings and machines are read again every minute, off the scheduler's
dispatching thread; machines that can't be used are looked at again as
often. No job of the account's is handed out until its settings were read
once. Each library's storage is looked at by the scan pass only, and jobs
go by that look (several jobs probing one mount at once would see it as
unreachable). What each look found is kept as when the library was last
seen and since when it's been unreachable; the scheduler's status carries
it over a restart, where it's a record, not a look (storage_seen).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from src.server.scheduler.scans import ScanState

logger = logging.getLogger(__name__)

# Settings and machines are read again this often (a new model reaches the
# scheduler within a minute)...
REFRESH_SEC = 60.0
# ...and sooner when the server didn't answer.
RETRY_SEC = 15.0


class Account:
    def __init__(self, tenant_id: str, client: Any, scan_state: ScanState, *, models: Any = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], datetime] | None = None) -> None:
        from src.processing.failure_report import FailureReport
        from src.processing.machine import current
        from src.processing.producer_settings import ProducerSettings
        from src.processing.proxy.analysis_cache import AnalysisProxyCache
        from src.producers import load
        from src.shared.ai_jobs import AI_JOBS

        self.tenant_id = tenant_id
        self.client = client
        self.scan_state = scan_state
        self.machine = current()  # this machine's way of processing (the scheduler's settings)
        # The GPU models, the scheduler's (every account's).
        self.models = models
        self._clock = clock
        if wall is None:
            from src.shared.utils import utcnow as wall
        self._wall = wall
        self.failures = FailureReport(client)
        self.producers = ProducerSettings(client, fetch=False)  # read by refresh()
        # Each AI job's machines (Settings → AI), checked and called by its guard.
        self.guards: dict[str, Any] = {name: load(job.guard)(client) for name, job in AI_JOBS.items()}
        self.analysis_cache = AnalysisProxyCache(client)
        # Set when the scheduler stops: jobs in hand save nothing more.
        self.stopping = threading.Event()
        # The server's settings were read at least once: its jobs can go.
        self.settings_ready = False
        # library_id -> the library; and its storage on this machine at the
        # last scan pass's look (None: not reachable).
        self.libraries: dict[str, dict] = {}
        self.roots: dict[str, Path | None] = {}
        # library_id -> {seen_at, away_since} (ISO times or None): when its
        # storage was last seen reachable, and since when it's been
        # unreachable (None: the latest look reached it); and the libraries
        # looked at since this start (a record from before isn't a look).
        self._seen: dict[str, dict[str, str | None]] = {}
        self._checked: set[str] = set()
        self._lock = threading.Lock()
        self._proxy_caches: dict[str, Any] = {}
        self._vision_provider: tuple[tuple, Any] | None = None
        self._transcriber: tuple[str, Any] | None = None
        self._refreshed_at: float | None = None
        self._job_checked_at: dict[str, float] = {}
        self._followed_at: float | None = None
        self.ready = {job: False for job in self.guards}

    def guard(self, job: str) -> Any:
        """The AI job's machines (src/processing/job_guard.py)."""
        return self.guards[job]

    # -- settings and machines ----------------------------------------------

    def follow_settings(self, artifact: str, settings_hash: str | None) -> None:
        """A redo is to be made with settings_hash: when that isn't what these
        settings make (an admin changed them since they were read), read them
        again now rather than within the minute, so the redo isn't made with
        the old ones. At most every RETRY_SEC, should they still differ."""
        from src.shared.producers import lineage

        if not settings_hash or lineage(artifact, self.producers.settings(artifact), None)["settings_hash"] == settings_hash:
            return
        now = self._clock()
        if self._followed_at is not None and now - self._followed_at < RETRY_SEC:
            return
        self._followed_at = now
        self.producers.refresh()

    def refresh(self, force: bool = False) -> None:
        """Read the settings and libraries again when due, and check the AI
        machines. Slow (it asks each machine): the scheduler calls it off its
        dispatching thread."""
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
        for job, guard in self.guards.items():
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

    def set_libraries(self, libraries: list[dict]) -> None:
        with self._lock:
            self.libraries = {lib["library_id"]: lib for lib in libraries}

    def set_reachable(self, roots: dict[str, Path | None]) -> None:
        """The scan pass's look at each library's storage."""
        now = self._wall().isoformat()
        with self._lock:
            self.roots = dict(roots)
            for library_id, root in roots.items():
                self._looked(library_id, root is not None, now)

    def _looked(self, library_id: str, reached: bool, now: str) -> None:
        seen = self._seen.setdefault(library_id, {"seen_at": None, "away_since": None})
        if reached:
            seen["seen_at"], seen["away_since"] = now, None
        elif seen["away_since"] is None:
            seen["away_since"] = now
        self._checked.add(library_id)

    def seed_storage(self, record: Mapping[str, Any] | None) -> None:
        """What the looks before a restart found (the last status's storage):
        when each library was last seen, until it's looked at again."""
        with self._lock:
            for library_id, seen in (record or {}).items():
                if library_id in self._seen or not isinstance(seen, Mapping):
                    continue
                self._seen[library_id] = {"seen_at": seen.get("seen_at"), "away_since": seen.get("away_since")}

    def storage_seen(self) -> dict[str, dict[str, Any]]:
        """Each library's {seen_at, away_since, checked}: checked when it was
        looked at since this start (otherwise it's the record from before)."""
        with self._lock:
            return {i: {**seen, "checked": i in self._checked} for i, seen in self._seen.items()
                    if not self.libraries or i in self.libraries}

    @property
    def reachable(self) -> dict[str, bool]:
        return {i: r is not None for i, r in self.roots.items()}

    def unreachable(self, library_id: str) -> None:
        """A job found the storage gone: storage work in it waits for the next look."""
        now = self._wall().isoformat()
        with self._lock:
            self.roots[library_id] = None
            self._looked(library_id, False, now)

    def storage_gone(self, library_id: str) -> bool:
        """The library's storage can't be read now, looked at as the scan pass
        does (with a timeout; an unmounted mount point is an empty folder)."""
        from src.processing.roots import reachable_root

        with self._lock:
            library = self.libraries.get(library_id)
        return library is None or reachable_root(library, require_entries=True) is None

    def library_ids(self, *, storage: bool) -> list[str]:
        """Libraries a kind's jobs can run in: those whose storage was reachable at the last look, when it reads originals."""
        with self._lock:
            return [i for i in self.libraries if not storage or self.roots.get(i) is not None]

    def root(self, library_id: str) -> Path | None:
        """The library's storage on this machine at the last look, or None."""
        with self._lock:
            return self.roots.get(library_id)

    def set_gpu_hold(self, hold: int) -> None:
        """Requests AI machines sharing this machine's GPU give up while video is decoded on it."""
        for guard in self.guards.values():
            guard.pool.set_gpu_hold(hold)

    def capacity(self, job: str) -> int:
        """Requests the job's online machines take at once (0 while none can be used)."""
        guard = self.guards[job]
        if not self.ready.get(job) or guard.down:
            return 0
        return guard.capacity()

    # -- caches and models --------------------------------------------------

    def proxy_cache(self, library_id: str) -> Any:
        """The images CLIP, faces and vision see, for clips of one library
        (made from the original while its storage is reachable)."""
        from src.processing.proxy.proxy_cache import ProxyCache
        from src.producers.clip import PROXY_CACHE_EDGE

        root = self.root(library_id)
        with self._lock:
            cache = self._proxy_caches.get(library_id)
            if cache is None or cache._root_path != root:
                cache = ProxyCache(max_edge=PROXY_CACHE_EDGE, root_path=root, client=self.client)
                self._proxy_caches[library_id] = cache
            return cache

    def vision_provider(self) -> tuple[Any, str]:
        """Describes and reads images on whichever vision machine is free, and the model."""
        vision = self.guards["vision"]
        model = vision.model
        vision_settings = self.producers.with_model("vision", model)
        ocr_settings = self.producers.with_model("ocr", model)
        key = (model, repr(sorted(vision_settings.items())), repr(sorted(ocr_settings.items())))
        with self._lock:
            if self._vision_provider is None or self._vision_provider[0] != key:
                self._vision_provider = (key, vision.provider(settings=vision_settings, ocr_settings=ocr_settings))
            return self._vision_provider[1], model

    def transcriber(self) -> Any:
        transcripts = self.guards["transcripts"]
        model = transcripts.model
        with self._lock:
            if self._transcriber is None or self._transcriber[0] != model:
                self._transcriber = (model, transcripts.transcriber())
            return self._transcriber[1]

    def close(self) -> None:
        self.stopping.set()
        self.failures.flush()
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass
