"""What one account's jobs run with (ADR-016 phase 4).

Its API client (the scheduler saves results through the API's own routes,
as the worker did: the same checks and lineage), the server's producer
settings, its AI machines (Settings → AI: a guard per AI job the producers
declare), the caches of proxies and analysis copies, and the scheduler's
models (CLIP, face detection: src/server/scheduler/models.py), which every
account shares.

Settings and machines are read again every minute, off the scheduler's
dispatching thread, and it checks in as the reader of the account's
libraries (check_in: ADR-018 phase 4; the answer is the libraries it
reads); machines that can't be used are looked at again as often. No job of the account's is handed out until its settings were read
once. Each library's storage is looked at by the scheduler itself, every
30 seconds and at once for a library not looked at since this start
(look_at_storage, off the dispatching thread), whatever is paused but
Pause all; the scan pass looks again right before it walks one. Jobs go
by the latest look; one that finds the storage gone puts that library's
storage work off until the next. What each look found is kept as when the
library was last seen and since when it's been unreachable; the
scheduler's status carries it over a restart, where it's a record, not a
look (storage_seen).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from src.processing.originals import Originals
from src.producers.names import DESCRIPTION, TEXT
from src.server.scheduler.kinds import SCAN_EVERY_SEC
from src.server.scheduler.scans import ScanState

logger = logging.getLogger(__name__)

# Settings and machines are read again this often (a new model reaches the
# scheduler within a minute)...
REFRESH_SEC = 60.0
# ...and sooner when the server didn't answer.
RETRY_SEC = 15.0
# Each library's storage is looked at this often (as often as the scan is
# offered): storage a job found gone is back in use within it.
LOOK_EVERY_SEC = SCAN_EVERY_SEC


class Account:
    def __init__(self, tenant_id: str, client: Any, scan_state: ScanState, *, models: Any = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], datetime] | None = None,
                 store: Any = None) -> None:
        """`store`: the API's Store, when it's on this machine's disk
        (service.stores_here): jobs read clips' previews and analysis proxies
        there, by key, and the caches hold no copies (inputs.py)."""
        from src.processing.failure_report import FailureReport
        from src.processing.machine import current
        from src.processing.producer_settings import ProducerSettings
        from src.processing.proxy.analysis_cache import AnalysisProxyCache
        from src.producers import load
        from src.server.scheduler.inputs import Inputs
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
        # What jobs' work is made from (Work.inputs).
        self.inputs = Inputs(self, store)
        # Set when the scheduler stops: jobs in hand save nothing more.
        self.stopping = threading.Event()
        # The server's settings were read at least once: its jobs can go.
        self.settings_ready = False
        # library_id -> the library; and its originals on this machine at the
        # latest look (src/processing/originals.py; None: not reachable).
        self.libraries: dict[str, dict] = {}
        self.reached: dict[str, Originals | None] = {}
        # When the scheduler last looked at every library's storage (look_at_storage).
        self._looked_at: float | None = None
        # library_id -> {seen_at, away_since} (ISO times or None): when its
        # storage was last seen reachable, and since when it's been
        # unreachable (None: the latest look reached it); and the libraries
        # looked at since this start (a record from before isn't a look).
        self._seen: dict[str, dict[str, str | None]] = {}
        self._checked: set[str] = set()
        self._lock = threading.Lock()
        self._proxy_cache: Any = None
        self._vision_provider: tuple[tuple, Any] | None = None
        self._transcriber: tuple[str, Any] | None = None
        self._refreshed_at: float | None = None
        self._job_checked_at: dict[str, float] = {}
        self._followed_at: float | None = None
        self.ready = {job: False for job in self.guards}
        # What its check-ins heard (check_in): each library's mark, and those a scan of which was asked for;
        # and the library being scanned now (the scan pass says), and what each library's latest scan has
        # left to send (to_upload), which they say.
        self.marks: dict[str, int] = {}
        self.scan_now: set[str] = set()
        self.scanning: str | None = None
        self.to_send: dict[str, tuple[int, int]] = {}

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
                self.check_in()
            except Exception:  # noqa: BLE001 — tried again at the next refresh
                logger.exception("scheduler: couldn't check in as %s's reader", self.tenant_id)
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

    def check_in(self) -> None:
        """Check in as the reader of the account's libraries (ADR-018
        decision 4, src/server/check_ins.py): how each is (scanning, idle,
        paused, or away when its storage wasn't reachable at the last look)
        and its last full scan. The answer is which libraries it reads, each
        with its mark and whether a scan of it was asked for. A mark that
        moved since has the settings read again at the next refresh, not
        within the minute. The scan pass checks in first too."""
        from datetime import UTC

        from src.server.check_ins import waiting
        from src.server.version import running
        from src.shared.readers import PROTOCOL

        with self._lock:
            said = [{"library_id": i, "state": self._state(lib), "mark": self.marks.get(i, 0),
                     **waiting(self.to_send.get(i)),
                     "last_full_scan_at": (datetime.fromtimestamp(full, UTC).isoformat()
                                           if (full := self.scan_state.last_full_scan.get(i)) is not None else None)}
                    for i, lib in self.libraries.items()]
        answer = self.client.post("/v1/readers/check-in", json={
            "reader": "server", "client": "this server", "version": running(), "protocol": PROTOCOL,
            "libraries": said}, retry=True).json()
        libraries = answer["libraries"]
        moved = any(self.marks.get(lib["library_id"], lib["mark"]) != lib["mark"] for lib in libraries)
        self.set_libraries(libraries)
        with self._lock:
            self.marks = {lib["library_id"]: lib["mark"] for lib in libraries}
            self.scan_now = {lib["library_id"] for lib in libraries if lib.get("scan_now")}
        if moved:
            self._refreshed_at = None

    def to_upload(self, library_id: str, files: int, size: int) -> None:
        """What the library's scan has found to add or read again and not
        sent yet (the scan pass says, as it goes): its check-ins say it until
        its next scan says otherwise, as a scan stopped part way leaves them
        to send."""
        with self._lock:
            self.to_send[library_id] = (files, size)

    def _state(self, library: dict) -> str:
        """What a check-in says of the library (under the lock)."""
        library_id = library["library_id"]
        if self.scanning == library_id:
            return "scanning"
        if library.get("scans_paused"):
            return "paused"
        if library_id in self._checked and self.reached.get(library_id) is None:
            return "away"
        return "idle"

    def reading(self) -> list[dict]:
        """The libraries it reads, as its latest check-in heard them."""
        with self._lock:
            return list(self.libraries.values())

    def set_libraries(self, libraries: list[dict]) -> None:
        """The account's libraries this server reads (src/shared/readers.py):
        the only ones whose storage it looks at and whose originals its
        producers read."""
        from src.shared.readers import read_by_server

        with self._lock:
            self.libraries = {lib["library_id"]: lib for lib in libraries if read_by_server(lib)}
            self.to_send = {i: left for i, left in self.to_send.items() if i in self.libraries}

    def look_at_storage(self) -> bool:
        """The scheduler's own look at each library's storage, when due:
        every LOOK_EVERY_SEC, and at once when a library wasn't looked at
        since this start (so before any of its storage work is handed out).
        Looked at as the scan pass does (originals.reach: with a timeout; an
        unmounted mount point is an empty folder), so the two never
        disagree. Storage work goes by it whatever is paused; the scheduler
        doesn't call it while Pause all is on (nothing touches the drives
        then). Slow (a share that sleeps takes its timeout): called off the
        dispatching thread. True when it looked."""
        from src.processing.originals import reach

        now = self._clock()
        with self._lock:
            libraries = list(self.libraries.values())
            new = any(i not in self._checked for i in self.libraries)
        if not new and self._looked_at is not None and now - self._looked_at < LOOK_EVERY_SEC:
            return False
        self._looked_at = now
        self.set_reachable({lib["library_id"]: reach(lib, require_entries=True) for lib in libraries})
        return True

    def set_reachable(self, found: Mapping[str, Originals | None]) -> None:
        """A look at libraries' storage (the scheduler's own, or the scan
        pass's before it walks one): each one's originals on this machine,
        None where they can't be read. Libraries it leaves out keep their
        last look."""
        now = self._wall().isoformat()
        with self._lock:
            self.reached.update(found)
            for library_id, originals in found.items():
                self._looked(library_id, originals is not None, now)

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
        return {i: r is not None for i, r in self.reached.items()}

    def unreachable(self, library_id: str) -> None:
        """A job found the storage gone: storage work in it waits for the next look."""
        now = self._wall().isoformat()
        with self._lock:
            self.reached[library_id] = None
            self._looked(library_id, False, now)

    def storage_gone(self, library_id: str) -> bool:
        """The library's storage can't be read now, looked at as the scan pass
        does (with a timeout; an unmounted mount point is an empty folder)."""
        from src.processing.originals import reach

        with self._lock:
            library = self.libraries.get(library_id)
        return library is None or reach(library, require_entries=True) is None

    def library_ids(self, *, storage: bool) -> list[str]:
        """Libraries a kind's jobs can run in: those whose storage was reachable at the last look, when it reads originals."""
        with self._lock:
            return [i for i in self.libraries if not storage or self.reached.get(i) is not None]

    def originals(self, library_id: str) -> Originals | None:
        """The library's originals on this machine at the last look, or None.
        Only a job's inputs read them (src/server/scheduler/inputs.py)."""
        with self._lock:
            return self.reached.get(library_id)

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

    def job_error(self, job: str) -> str:
        """Why the job's machines can't be used now, as its guard says it (the
        scheduler's status carries it to Processing)."""
        return self.guards[job].error or "Its machines haven't been checked yet."

    # -- caches and models --------------------------------------------------

    def proxy_cache(self, library_id: str) -> Any:
        """The previews CLIP, faces and vision see where the API doesn't keep
        them on this machine (inputs.py): this machine's cache, downloaded into
        from the API. Never made from an original (ADR-020 decision 4)."""
        from src.processing.proxy.proxy_cache import ProxyCache
        from src.producers.clip import PROXY_CACHE_EDGE

        with self._lock:
            if self._proxy_cache is None:
                self._proxy_cache = ProxyCache(max_edge=PROXY_CACHE_EDGE, client=self.client)
            return self._proxy_cache

    def vision_provider(self) -> tuple[Any, str]:
        """Describes and reads images on whichever vision machine is free, and the model."""
        vision = self.guards["vision"]
        model = vision.model
        vision_settings = self.producers.with_model(DESCRIPTION, model)
        ocr_settings = self.producers.with_model(TEXT, model)
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
