"""The scheduler service: the brain runs all processing (ADR-016 phase 4).

One process beside the API (systemd: lumiverb-scheduler), sharing its
database. Every second or so it:

1. tops up each kind's queue from each account's database (queue.py),
   and offers each account's scan look every 30 seconds;
2. fills every free slot of every pool with the best job for it
   (dispatch.py: tier, then oldest first), run on a thread;
3. collects finished jobs.

Pools are sized from this machine's config and each account's AI machines
(Settings → AI): a job whose machines can't be used isn't handed out, and
no clip is charged for it. Jobs save their results through the API's
routes on the brain (runners.py). A crash restarts the scheduler, never the
site: ffmpeg, Whisper and face detection run in their own processes.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from src.server.scheduler.dispatch import Dispatcher, Job
from src.server.scheduler.kinds import AI_JOB_KINDS, KINDS, QUEUED
from src.server.scheduler.queue import BUFFER

logger = logging.getLogger(__name__)

TICK_SEC = 1.0
# How long a stop waits for jobs in hand before leaving them to the next start.
STOP_GRACE_SEC = 25.0
# Failures are sent to the server at least this often.
FLUSH_EVERY_SEC = 10.0

# Kinds whose AI job decides whether they're handed out.
_JOB_OF = {kind: job for job, kinds in AI_JOB_KINDS.items() for kind in kinds}


def default_capacity(cfg: Any) -> dict[str, int]:
    """The shared pools' slots: today's limits, which the brain handles."""
    from src.client.cli.repair import default_render_concurrency

    return {
        "scan": 1,  # each scan is parallel inside
        "probe": 2,
        "render": cfg.render_concurrency or default_render_concurrency(),
        "clip": 2,
        "faces": 1,  # one subprocess, batches of 25
        "scenes": 1,
    }


class Scheduler:
    def __init__(
        self,
        accounts: Callable[[], Mapping[str, Any]],
        *,
        capacity: Mapping[str, int],
        candidates: Callable[[str, Any, list[str]], list[dict]] | None = None,
        runners: Mapping[str, Callable[..., None]] | None = None,
        scan: Callable[..., None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        max_threads: int = 64,
    ) -> None:
        from src.server.scheduler import runners as runner_mod

        self._accounts = accounts
        self._candidates = candidates or _from_database
        self._runners = dict(runners or runner_mod.RUNNERS)
        self._scan = scan or runner_mod.scan
        self._clock = clock
        self._wall = wall
        self.dispatcher = Dispatcher({k.name: k.spec for k in KINDS.values()}, capacity, clock=clock)
        self._pool = ThreadPoolExecutor(max_workers=max_threads, thread_name_prefix="job")
        self._running: dict[Future, Job] = {}
        self._flushed_at = clock()

    # -- one tick -----------------------------------------------------------

    def tick(self) -> int:
        """Top up the queue, start what fits, collect what's done. Returns the jobs started."""
        accounts = dict(self._accounts())
        for tenant_id, acct in accounts.items():
            try:
                self._top_up(tenant_id, acct)
            except Exception:  # noqa: BLE001 — one account's trouble doesn't stop the others
                logger.exception("scheduler: looking at %s's work failed", tenant_id)
        started = 0
        for pool in self.dispatcher.pools(list(accounts)):
            while (job := self.dispatcher.take(pool)) is not None:
                acct = accounts.get(job.tenant_id)
                if acct is None:
                    self.dispatcher.done(job)
                    continue
                self._running[self._pool.submit(self._run, acct, job)] = job
                started += 1
        self.collect()
        if self._clock() - self._flushed_at >= FLUSH_EVERY_SEC:
            self._flushed_at = self._clock()
            for acct in accounts.values():
                acct.failures.flush()
        return started

    def _top_up(self, tenant_id: str, acct: Any) -> None:
        acct.refresh()
        for job in AI_JOB_KINDS:
            slots = acct.capacity(job)
            # Transcripts: twice the machines', so each clip's audio is got
            # ready while the machines work on others (they hold their own limits).
            self.dispatcher.set_capacity(f"{job}@{tenant_id}", slots if job == "vision" else 2 * slots)
            if not slots:
                for kind in AI_JOB_KINDS[job]:
                    self.dispatcher.clear(tenant_id, kind)
        if self.dispatcher.wanted(tenant_id, "scan"):
            self.dispatcher.offer(tenant_id, "scan", [{"asset_id": f"scan:{tenant_id}", "created_at": ""}],
                                  complete=False)
        for kind in QUEUED:
            job = _JOB_OF.get(kind.name)
            if job is not None and not acct.capacity(job):
                continue
            if not self.dispatcher.wanted(tenant_id, kind.name):
                continue
            libraries = acct.library_ids(storage=kind.storage)
            items = self._candidates(tenant_id, kind, libraries) if libraries else []
            self.dispatcher.offer(tenant_id, kind.name, items, complete=len(items) < BUFFER)

    def _run(self, acct: Any, job: Job) -> None:
        try:
            if job.kind == "scan":
                self._scan(acct, job, now=self._wall())
            else:
                self._runners[job.kind](acct, job)
        except Exception:  # noqa: BLE001 — a job's surprise is logged; its clips are tried again later
            logger.exception("scheduler: %s for %s failed", job.kind, ", ".join(job.asset_ids[:3]))

    def collect(self, timeout: float | None = 0) -> None:
        """Free the slots of finished jobs (waiting up to timeout for one)."""
        from concurrent.futures import FIRST_COMPLETED, wait

        if not self._running:
            return
        done, _ = wait(list(self._running), timeout=timeout, return_when=FIRST_COMPLETED)
        for future in done:
            self.dispatcher.done(self._running.pop(future))

    @property
    def running(self) -> int:
        return len(self._running)

    def stop(self, grace: float = STOP_GRACE_SEC) -> None:
        """Wait for jobs in hand up to grace; the rest are left to the next start."""
        deadline = self._clock() + grace
        while self._running and self._clock() < deadline:
            self.collect(timeout=min(1.0, max(0.0, deadline - self._clock())))
        self._pool.shutdown(wait=False, cancel_futures=True)


_tenant_urls: dict[str, str] = {}


def tenant_url(tenant_id: str) -> str:
    """The account's database, as its routing says (what requests use)."""
    if tenant_id not in _tenant_urls:
        from src.server.database import get_control_session
        from src.server.repository.control_plane import TenantDbRoutingRepository

        with get_control_session() as ctrl:
            routing = TenantDbRoutingRepository(ctrl).get_by_tenant_id(tenant_id)
            if routing is None:
                raise LookupError(f"no database for {tenant_id}")
            _tenant_urls[tenant_id] = routing.connection_string
    return _tenant_urls[tenant_id]


def _from_database(tenant_id: str, kind: Any, libraries: list[str]) -> list[dict]:
    from sqlmodel import Session

    from src.server.database import get_engine_for_url
    from src.server.scheduler.queue import candidates

    with Session(get_engine_for_url(tenant_url(tenant_id))) as session:
        return candidates(session, kind, libraries)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

KEY_LABEL = "scheduler"


def api_url() -> str:
    """Where the scheduler reaches the API: LUMIVERB_API_URL, else the API's own port on this machine."""
    if url := os.environ.get("LUMIVERB_API_URL"):
        return url.rstrip("/")
    port = os.environ.get("API_PORT")
    if port:
        host = os.environ.get("API_LISTEN_HOST", "127.0.0.1")
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return f"http://{host}:{port}"
    from src.client.cli.config import get_api_url

    return get_api_url()


def scheduler_key(tenant_id: str) -> str:
    """A key for the scheduler's calls on the account's behalf: made at
    start, the previous one revoked. Nothing to configure."""
    from src.server.database import get_control_session
    from src.server.repository.control_plane import ApiKeyRepository

    with get_control_session() as ctrl:
        repo = ApiKeyRepository(ctrl)
        for key in repo.list_by_tenant(tenant_id):
            if key.label == KEY_LABEL and key.revoked_at is None:
                repo.revoke(key.key_id, tenant_id)
        _, plaintext = repo.create(tenant_id, label=KEY_LABEL, role="admin")
    return plaintext


class Accounts:
    """Every active account, each with what its jobs run with; new accounts
    are picked up every few minutes."""

    LIST_EVERY_SEC = 300.0

    def __init__(self, scan_state: Any, *, url: str | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._scan_state = scan_state
        self._url = url or api_url()
        self._clock = clock
        self._accounts: dict[str, Any] = {}
        self._listed_at: float | None = None

    def __call__(self) -> dict[str, Any]:
        now = self._clock()
        if self._listed_at is None or now - self._listed_at >= self.LIST_EVERY_SEC:
            self._listed_at = now
            self._list()
        return self._accounts

    def _list(self) -> None:
        from src.client.cli.client import LumiverbClient
        from src.server.database import get_control_session
        from src.server.repository.control_plane import TenantRepository
        from src.server.scheduler.account import Account

        with get_control_session() as ctrl:
            active = [t.tenant_id for t in TenantRepository(ctrl).list_all() if t.status == "active"]
        for tenant_id in list(self._accounts):
            if tenant_id not in active:
                self._accounts.pop(tenant_id).close()
        for tenant_id in active:
            if tenant_id in self._accounts:
                continue
            try:
                client = LumiverbClient(base_url=self._url, token=scheduler_key(tenant_id))
                self._accounts[tenant_id] = Account(tenant_id, client, self._scan_state)
                logger.info("scheduler: working for %s", tenant_id)
            except Exception:  # noqa: BLE001 — tried again at the next listing
                logger.exception("scheduler: couldn't start work for %s", tenant_id)

    def close(self) -> None:
        for acct in self._accounts.values():
            acct.close()


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


def run(*, stop: threading.Event, scheduler: Scheduler, save: Callable[[], None] | None = None,
        tick_sec: float = TICK_SEC) -> None:
    """Tick until stopped, then let the jobs in hand finish (up to a grace period)."""
    while not stop.is_set():
        try:
            scheduler.tick()
        except Exception:  # noqa: BLE001 — anything else is worth its traceback, not a stop
            logger.exception("scheduler: a tick failed")
        if save is not None:
            save()
        if scheduler.running:
            scheduler.collect(timeout=tick_sec)
        else:
            stop.wait(tick_sec)
    scheduler.stop()
    if save is not None:
        save()


def main() -> int:
    """lumiverb-scheduler: run every account's processing on this machine."""
    from src.client.cache_dir import cache_dir
    from src.client.cli.config import load_config
    from src.client.proxy.analysis_cache import clear_leftovers
    from src.server.scheduler.scans import STATE_FILE, ServiceLock, load_state, save_state, saved_form

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    lock = ServiceLock()
    if not lock.acquire():
        logger.error("scheduler: another scheduler (or the old worker) is running on this machine")
        return 1
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        if removed := clear_leftovers():
            logger.info("scheduler: removed %d half-made analysis proxies left by an earlier run", removed)
        state_path = cache_dir(STATE_FILE)
        state = load_state(state_path)
        last = [saved_form(state)]

        def save() -> None:
            if (now := saved_form(state)) != last[0]:
                save_state(state_path, now)
                last[0] = now

        accounts = Accounts(state)
        scheduler = Scheduler(accounts, capacity=default_capacity(load_config()))
        logger.info("scheduler: started")
        try:
            run(stop=stop, scheduler=scheduler, save=save)
        finally:
            accounts.close()
        logger.info("scheduler: stopped")
        return 0
    finally:
        lock.release()
