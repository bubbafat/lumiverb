"""The scheduler service: the brain runs all processing (ADR-016 phase 4).

One process beside the API (systemd: lumiverb-scheduler), sharing its
database. Every second or so it:

1. has each account's queue topped up, off this thread (refill): its
   settings and AI machines read again when due, and each kind's due clips
   listed from its database (queue.py), less those in hand or just tried;
   each account's scan look is offered every 30 seconds;
2. fills every free slot of every pool with the best job for it
   (dispatch.py: tier, then oldest first), run on a thread;
3. collects finished jobs.

Pools are sized from this machine's config and each account's AI machines
(Settings → AI): a job whose machines can't be used isn't handed out, and
no clip is charged for it. Jobs save their results through the API's
routes on the brain (runners.py). A crash restarts the scheduler, never the
site; ffmpeg and face detection run in their own processes. One scheduler
runs at a time, on any machine (a lock in the control-plane database).
"""

from __future__ import annotations

import itertools
import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any

from src.server.scheduler.dispatch import Dispatcher, Job, Outcome
from src.server.scheduler.kinds import AI_JOB_KINDS, KINDS, QUEUED
from src.server.scheduler.queue import BUFFER
from src.shared.producers import PAUSE_SCANS, pause_targets

logger = logging.getLogger(__name__)

TICK_SEC = 1.0
# How long a stop waits for jobs in hand (which save nothing more) before
# leaving them to the next start.
STOP_GRACE_SEC = 25.0
# Failures are sent to the server at least this often.
FLUSH_EVERY_SEC = 10.0
# What the scheduler is doing is written for Settings → Processing this often.
STATUS_EVERY_SEC = 5.0
# What an admin paused is read this often, apart from the refill (which can be slow).
HOLD_EVERY_SEC = 1.0
# The control-plane lock only one scheduler holds ("lumv"), and how often
# it's checked that the lock is still held.
LOCK_ID = 0x6C756D76
LOCK_CHECK_SEC = 60.0

# A save the API refused for what's in it (not who asked, or a clip gone meanwhile).
_REFUSED = {400, 413, 422}

# Kinds whose AI job decides whether they're handed out.
_JOB_OF = {kind: job for job, kinds in AI_JOB_KINDS.items() for kind in kinds}


def default_capacity(cfg: Any) -> dict[str, int]:
    """The shared pools' slots: today's limits, which the brain handles; a
    pool only a producer names gets the slots it declares."""
    from src.client.cli.repair import default_render_concurrency
    from src.shared.producers import PRODUCERS

    sized = {
        "scan": 1,  # each scan is parallel inside
        "probe": 2,
        "render": cfg.render_concurrency or default_render_concurrency(),
        # CLIP and face detection take turns on this machine's GPU, beside
        # the decodes and the AI machine that may share it.
        "gpu": 1,
        "scenes": 1,
    }
    for p in PRODUCERS.values():
        if p.scheduled and not p.per_account and p.pool not in sized:
            sized[p.pool] = p.slots
    return sized


class Scheduler:
    def __init__(
        self,
        accounts: Callable[[], Mapping[str, Any]],
        *,
        capacity: Mapping[str, int],
        candidates: Callable[..., list[dict] | None] | None = None,
        paused: Callable[[str], set[str]] | None = None,
        on_hold: Callable[[str], set[str]] | None = None,
        retry_requested: Callable[[str], str | None] | None = None,
        runners: Mapping[str, Callable[..., Outcome]] | None = None,
        scan: Callable[..., None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        max_threads: int = 64,
        inline_refill: bool = False,
        gpu_decodes: int = 0,
        write_status: Callable[[str, dict], None] | None = None,
        last_status: Callable[[str], dict | None] | None = None,
    ) -> None:
        from src.server.scheduler import runners as runner_mod

        self._accounts = accounts
        self._candidates = candidates or _from_database
        self._paused = paused or _paused_in_database
        self._on_hold = on_hold or _on_hold_in_database
        self._retry_requested = retry_requested or _retry_requested_in_database
        self._retry_seen: dict[str, str | None] = {}
        # Each account's last status, read once: its paces start there.
        self._last_status = last_status or _status_from_database
        self._paced: set[str] = set()
        self._seeded: dict[str, Any] = {}
        self._runners = dict(runners or runner_mod.runners())
        self._scan = scan or runner_mod.scan
        self._clock = clock
        self._wall = wall
        self.dispatcher = Dispatcher({k.name: k.spec for k in KINDS.values()}, capacity, clock=clock)
        self._pool = ThreadPoolExecutor(max_workers=max_threads, thread_name_prefix="job")
        self._running: dict[Future, Job] = {}
        self._flushed_at = clock()
        # Refills run off the dispatching thread (they ask the database and
        # the AI machines), one at a time per account.
        self._inline_refill = inline_refill
        self._refill_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="refill")
        self._refilling: dict[str, Future] = {}
        # Renders that decode on this machine's GPU at once (0: they don't):
        # while they run, AI machines sharing that GPU take fewer requests.
        self._gpu_decodes = gpu_decodes
        self.gpu_hold = 0
        self._write_status = write_status or _status_to_database
        self._status_at: dict[str, float] = {}
        self._flushing: dict[str, Future] = {}
        # What's paused is read on its own threads, so a slow refill doesn't hold it up.
        self._hold_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hold")
        self._holding: dict[str, Future] = {}
        self._hold_at: dict[str, float] = {}
        self._hold_unread: set[str] = set()
        self._hold_reads = itertools.count()  # which read of what's paused is newest

    # -- one tick -----------------------------------------------------------

    def tick(self) -> int:
        """Top up the queue, start what fits, collect what's done. Returns the jobs started."""
        accounts = dict(self._accounts())
        for tenant_id, acct in accounts.items():
            self._read_hold_soon(tenant_id)
            self._refill_soon(tenant_id, acct)
        started = 0
        for pool in self.dispatcher.pools(list(accounts)):
            while (job := self.dispatcher.take(pool)) is not None:
                acct = accounts.get(job.tenant_id)
                if acct is None:
                    self.dispatcher.done(job, tried=False)
                    continue
                self._running[self._pool.submit(self._run_job, acct, job)] = job
                started += 1
        self.collect()
        self._share_the_gpu(accounts)
        if self._clock() - self._flushed_at >= FLUSH_EVERY_SEC:
            self._flushed_at = self._clock()
            for acct_id, acct in accounts.items():
                # Off this thread: a slow server mustn't hold up the dispatching.
                if self._inline_refill:
                    acct.failures.flush()
                elif (flushing := self._flushing.get(acct_id)) is None or flushing.done():
                    self._flushing[acct_id] = self._refill_pool.submit(acct.failures.flush)
        return started

    def _share_the_gpu(self, accounts: Mapping[str, Any]) -> None:
        """Video work comes first on this machine's GPU (Robert, Oct 9): each
        render decoding there, and scene detection, takes a request from the
        AI machines that share it, down to none."""
        if self._gpu_decodes <= 0:
            hold = 0
        else:
            hold = min(self._gpu_decodes, self.dispatcher.running("render")) + min(1, self.dispatcher.running("scenes"))
        self.gpu_hold = hold
        for acct in accounts.values():
            set_hold = getattr(acct, "set_gpu_hold", None)
            if set_hold is not None:
                set_hold(hold)

    def status(self, tenant_id: str) -> dict:
        """What the scheduler is doing for the account now (Settings → Processing)."""
        from src.shared.utils import utcnow

        return {"at": utcnow().isoformat(), "gpu_hold": self.gpu_hold, **self.dispatcher.status(tenant_id)}

    def _read_hold_soon(self, tenant_id: str) -> None:
        """Every second or so, off this thread: what an admin paused (a refill reads it too)."""
        if self._inline_refill or self._clock() - self._hold_at.get(tenant_id, float("-inf")) < HOLD_EVERY_SEC:
            return
        reading = self._holding.get(tenant_id)
        if reading is None or reading.done():
            self._hold_at[tenant_id] = self._clock()
            self._holding[tenant_id] = self._hold_pool.submit(self._read_hold, tenant_id)

    def _read_hold(self, tenant_id: str) -> set[str]:
        """The pause switches an admin paused: PAUSE_SCANS, PAUSE_UPKEEP or producers'
        artifacts. The dispatcher hands out none of it from now on; what's
        running finishes. Until it can be read, as if every switch is paused."""
        seq = next(self._hold_reads)
        try:
            held = set(self._on_hold(tenant_id))
            if tenant_id in self._hold_unread:
                self._hold_unread.discard(tenant_id)
                logger.warning("scheduler: what %s paused can be read again", tenant_id)
        except Exception:  # noqa: BLE001
            if tenant_id not in self._hold_unread:  # once, not every second
                self._hold_unread.add(tenant_id)
                logger.exception("scheduler: reading what %s paused failed; nothing starts until it can be", tenant_id)
            held = set(pause_targets())
        self.dispatcher.hold(tenant_id, {k.name for k in KINDS.values()
                                         if (k.flag and k.artifact in held)
                                         or (k.name == "scan" and PAUSE_SCANS in held)}, seq=seq)
        return held

    def _refill_soon(self, tenant_id: str, acct: Any) -> None:
        if self._inline_refill:
            self._refill(tenant_id, acct)
            return
        running = self._refilling.get(tenant_id)
        if running is None or running.done():
            self._refilling[tenant_id] = self._refill_pool.submit(self._refill, tenant_id, acct)

    def _refill(self, tenant_id: str, acct: Any) -> None:
        """Read the account's settings and machines when due, size its AI
        pools, list what's due for each kind that wants more, and say what's
        running (every few seconds). What an admin paused hands out nothing
        more, at once; what's running finishes."""
        held = self._read_hold(tenant_id)
        try:
            acct.refresh()
        except Exception:  # noqa: BLE001 — tried again at the next tick
            logger.exception("scheduler: refreshing %s failed", tenant_id)
        if self._seeded.get(tenant_id) is not acct:
            # The last status: each kind's pace (once), and what the looks at
            # each library's storage found before this start (each new account).
            self._seeded[tenant_id] = acct
            try:
                last = self._last_status(tenant_id) or {}
                if tenant_id not in self._paced:
                    self._paced.add(tenant_id)
                    self.dispatcher.seed_pace(tenant_id, last.get("pace") or {})
                acct.seed_storage(last.get("storage"))
            except Exception:  # noqa: BLE001 — it learns them again
                logger.exception("scheduler: reading %s's last status failed", tenant_id)
        if self._clock() - self._status_at.get(tenant_id, float("-inf")) >= STATUS_EVERY_SEC:
            self._status_at[tenant_id] = self._clock()
            try:
                # Libraries whose work on the originals waits: not reachable at
                # the latest look, or not looked at since this start.
                waits = sorted(set(acct.library_ids(storage=False)) - set(acct.library_ids(storage=True)))
                self._write_status(tenant_id, {**self.status(tenant_id), "storage_waits": waits,
                                               "storage": acct.storage_seen()})
            except Exception:  # noqa: BLE001 — only a view of it
                logger.exception("scheduler: writing %s's status failed", tenant_id)
        for job in AI_JOB_KINDS:
            slots = acct.capacity(job)
            # Transcripts: twice the machines', so each clip's audio is got
            # ready while the machines work on others (they hold their own limits).
            self.dispatcher.set_capacity(f"{job}@{tenant_id}", slots if job == "vision" else 2 * slots)
            if not slots:
                for kind in AI_JOB_KINDS[job]:
                    self.dispatcher.clear(tenant_id, kind)
        # Failing clips someone asked to try again aren't held back by the
        # hour this scheduler keeps a clip it just tried.
        try:
            asked = self._retry_requested(tenant_id)
        except Exception:  # noqa: BLE001
            logger.exception("scheduler: reading %s's retry requests failed", tenant_id)
            asked = self._retry_seen.get(tenant_id)
        if tenant_id in self._retry_seen and asked != self._retry_seen[tenant_id]:
            self.dispatcher.forget_taken(tenant_id)
        self._retry_seen[tenant_id] = asked
        # A redo an admin stopped hands out nothing more, at once.
        try:
            stopped = self._paused(tenant_id)
        except Exception:  # noqa: BLE001 — as if stopped, until it can be read
            logger.exception("scheduler: reading %s's stopped redos failed", tenant_id)
            stopped = {k.artifact for k in QUEUED if k.redo}
        for kind in QUEUED:
            if kind.redo and kind.artifact in stopped:
                self.dispatcher.clear(tenant_id, kind.name)
        if PAUSE_SCANS not in held and self.dispatcher.wanted(tenant_id, "scan"):
            self.dispatcher.offer(tenant_id, "scan", [{"asset_id": f"scan:{tenant_id}", "created_at": ""}],
                                  complete=False)
        if not acct.settings_ready:
            return  # nothing is made with settings the server hasn't said
        for kind in QUEUED:
            job = _JOB_OF.get(kind.name)
            if job is not None and not acct.capacity(job):
                continue
            if kind.artifact in held or (kind.redo and kind.artifact in stopped):
                continue
            if not self.dispatcher.wanted(tenant_id, kind.name):
                continue
            libraries = acct.library_ids(storage=kind.storage)
            if not libraries:  # nothing to ask yet (no storage reachable): not an empty answer to wait on
                self.dispatcher.clear(tenant_id, kind.name)
                continue
            try:
                items = self._candidates(tenant_id, kind, libraries, self.dispatcher.held(tenant_id, kind.name))
            except Exception:  # noqa: BLE001 — one kind's trouble doesn't hold up the others
                logger.exception("scheduler: listing %s for %s failed", kind.name, tenant_id)
                items = []
            if items is None:  # paused (or its redo stopped) since this refill looked: not an empty answer
                self.dispatcher.clear(tenant_id, kind.name)
                continue
            if kind.redo and items:
                acct.follow_settings(kind.artifact, items[0].get("settings_hash"))
            self.dispatcher.offer(tenant_id, kind.name, items, complete=len(items) < BUFFER)

    def _run_job(self, acct: Any, job: Job) -> tuple[Outcome, set[str]]:
        """Run the job; its outcome, and which of its clips failures were
        charged to meanwhile (its kind learns its pace from the rest)."""
        failures = getattr(acct, "failures", None)
        mark = failures.mark() if hasattr(failures, "mark") else None
        outcome = self._run(acct, job)
        kind = KINDS.get(job.kind)
        if mark is None or kind is None or not kind.flag:
            return outcome, set()
        try:
            return outcome, set(failures.charged(kind.artifact, job.asset_ids, since=mark))
        except Exception:  # noqa: BLE001 — only the pace's to lose
            return outcome, set()

    def _run(self, acct: Any, job: Job) -> Outcome:
        try:
            if job.kind == "scan":
                self._scan(acct, job, now=self._wall())
                return None
            return self._runners[KINDS[job.kind].base](acct, job)
        except Exception as e:  # noqa: BLE001 — a job's surprise is logged; its clips are tried again later
            if getattr(e, "status_code", None) in _REFUSED and KINDS[job.kind].flag:  # a clip's (not a scan's)
                # The API can't take what was made: the clip's failure, reported
                # (the server says when to try again), not made again every hour.
                logger.warning("scheduler: the server refused %s for %s: %s", job.kind, ", ".join(job.asset_ids[:3]), e)
                for asset_id in job.asset_ids:
                    acct.failures.add(KINDS[job.kind].artifact, asset_id, f"The server refused it: {e}")
                return None
            logger.exception("scheduler: %s for %s failed", job.kind, ", ".join(job.asset_ids[:3]))
            return list(job.asset_ids)  # none saved or reported: each waits the long while

    def collect(self, timeout: float | None = 0) -> None:
        """Free the slots of finished jobs (waiting up to timeout for one)."""
        from src.server.scheduler.runners import NOT_TRIED

        if not self._running:
            return
        done, _ = wait(list(self._running), timeout=timeout, return_when=FIRST_COMPLETED)
        for future in done:
            job = self._running.pop(future)
            try:
                outcome, failed = future.result()
            except Exception:  # noqa: BLE001 — _run catches its own
                outcome, failed = list(job.asset_ids), set()
            if outcome == NOT_TRIED:
                self.dispatcher.done(job, tried=False)
            else:
                self.dispatcher.done(job, waiting=outcome or (), failed=failed)

    @property
    def running(self) -> int:
        return len(self._running)

    def stop(self, accounts: Mapping[str, Any] | None = None, grace: float = STOP_GRACE_SEC) -> None:
        """Jobs in hand save nothing more; wait for them up to grace, and
        leave the rest to the next start."""
        for acct in (accounts or {}).values():
            acct.stopping.set()
        deadline = self._clock() + grace
        while self._running and self._clock() < deadline:
            self.collect(timeout=min(1.0, max(0.0, deadline - self._clock())))
        self._refill_pool.shutdown(wait=False, cancel_futures=True)
        self._hold_pool.shutdown(wait=False, cancel_futures=True)
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


def _from_database(tenant_id: str, kind: Any, libraries: list[str], skip: list[str] | None = None) -> list[dict] | None:
    """The kind's due clips, oldest first; None when it was paused (or its
    redo stopped) since the scheduler last looked."""
    from sqlmodel import Session

    from src.server.database import get_engine_for_url
    from src.server.repository import lineage
    from src.server.scheduler.queue import candidates

    with Session(get_engine_for_url(tenant_url(tenant_id))) as session:
        if kind.artifact in lineage.processing_paused(session):
            return None
        if not kind.redo:
            return candidates(session, kind, libraries, skip=skip)
        if kind.artifact in lineage.paused(session):
            return None
        return candidates(session, kind, libraries, skip=skip,
                          want=lineage.desired(session, kind.artifact, _job_models(tenant_id)))


def _job_models(tenant_id: str) -> dict[str, str]:
    """The account's model per AI job (Settings → AI)."""
    from src.server.repository.ai_machines import account_job_models

    return account_job_models(tenant_id)


def _retry_requested_in_database(tenant_id: str) -> str | None:
    """When someone last asked for failing clips to be tried again (lineage.retry)."""
    from sqlalchemy import text
    from sqlmodel import Session

    from src.server.database import get_engine_for_url

    with Session(get_engine_for_url(tenant_url(tenant_id))) as session:
        return session.execute(text("SELECT value FROM system_metadata WHERE key = 'scheduler.retry_at'")).scalar()


def _status_to_database(tenant_id: str, status: dict) -> None:
    import json

    from sqlalchemy import text
    from sqlmodel import Session

    from src.server.database import get_engine_for_url

    with Session(get_engine_for_url(tenant_url(tenant_id))) as session:
        session.execute(text(
            "INSERT INTO system_metadata (key, value, updated_at) VALUES ('scheduler.status', :v, now())"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"), {"v": json.dumps(status)})
        session.commit()


def _on_hold_in_database(tenant_id: str) -> set[str]:
    """The pause switches an admin paused (lineage.processing_paused)."""
    from sqlmodel import Session

    from src.server.database import get_engine_for_url
    from src.server.repository import lineage

    with Session(get_engine_for_url(tenant_url(tenant_id))) as session:
        return set(lineage.processing_paused(session))


def _status_from_database(tenant_id: str) -> dict | None:
    """The status the scheduler last wrote for the account (its paces, its storage looks)."""
    import json

    from sqlalchemy import text
    from sqlmodel import Session

    from src.server.database import get_engine_for_url

    with Session(get_engine_for_url(tenant_url(tenant_id))) as session:
        raw = session.execute(text("SELECT value FROM system_metadata WHERE key = 'scheduler.status'")).scalar()
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _paused_in_database(tenant_id: str) -> set[str]:
    from sqlmodel import Session

    from src.server.database import get_engine_for_url
    from src.server.repository import lineage

    with Session(get_engine_for_url(tenant_url(tenant_id))) as session:
        return set(lineage.paused(session))


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


def scheduler_client(url: str, key: str) -> Any:
    """An API client that notes when its key stops working (someone revoked
    it), so the account gets a new one."""
    from src.client.cli.client import LumiverbClient

    class SchedulerClient(LumiverbClient):
        unauthorized = False

        def _handle_response(self, response):  # type: ignore[no-untyped-def]
            if response.status_code == 401:
                self.unauthorized = True
            return super()._handle_response(response)

    return SchedulerClient(base_url=url, token=key)


class Accounts:
    """Every active account, each with what its jobs run with; new accounts
    are picked up every few minutes, and one whose key stopped working is
    started again with a new one."""

    LIST_EVERY_SEC = 300.0
    # A listing that failed (the control plane away) is tried again sooner.
    LIST_RETRY_SEC = 30.0

    def __init__(self, scan_state: Any, *, url: str | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._scan_state = scan_state
        self._url = url or api_url()
        self._clock = clock
        self._accounts: dict[str, Any] = {}
        self._listed_at: float | None = None
        self._lock = threading.Lock()

    def __call__(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            revoked = [t for t, acct in self._accounts.items() if getattr(acct.client, "unauthorized", False)]
            for tenant_id in revoked:
                logger.warning("scheduler: %s's key stopped working; making a new one", tenant_id)
                self._accounts.pop(tenant_id).close()
            if revoked or self._listed_at is None or now - self._listed_at >= self.LIST_EVERY_SEC:
                try:
                    self._list()
                    self._listed_at = now
                except Exception:  # noqa: BLE001 — the accounts already known go on
                    logger.exception("scheduler: listing the accounts failed; trying again shortly")
                    self._listed_at = now - self.LIST_EVERY_SEC + self.LIST_RETRY_SEC
            return dict(self._accounts)

    def _list(self) -> None:
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
                client = scheduler_client(self._url, scheduler_key(tenant_id))
                self._accounts[tenant_id] = Account(tenant_id, client, self._scan_state)
                logger.info("scheduler: working for %s", tenant_id)
            except Exception:  # noqa: BLE001 — tried again at the next listing
                logger.exception("scheduler: couldn't start work for %s", tenant_id)

    def all(self) -> dict[str, Any]:
        return dict(self._accounts)

    def close(self) -> None:
        for acct in self._accounts.values():
            acct.close()


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


def run(*, stop: threading.Event, scheduler: Scheduler, save: Callable[[], None] | None = None,
        tick_sec: float = TICK_SEC, accounts: Callable[[], Mapping[str, Any]] | None = None,
        holds_lock: Callable[[], bool] | None = None, lock_check_sec: float = LOCK_CHECK_SEC) -> bool:
    """Tick until stopped, then let the jobs in hand finish (up to a grace
    period). False when it stopped because the database lock was lost."""
    checked_at = time.monotonic()
    lost = False

    def save_now() -> None:
        if save is None:
            return
        try:
            save()
        except Exception:  # noqa: BLE001 — tried again next tick
            logger.exception("scheduler: saving the scan state failed")

    while not stop.is_set():
        if holds_lock is not None and time.monotonic() - checked_at >= lock_check_sec:
            checked_at = time.monotonic()
            if not holds_lock():
                logger.error("scheduler: the control-plane lock was lost (the database restarted?); stopping")
                lost = True
                stop.set()
                break
        try:
            scheduler.tick()
        except Exception:  # noqa: BLE001 — anything else is worth its traceback, not a stop
            logger.exception("scheduler: a tick failed")
        save_now()
        if scheduler.running:
            scheduler.collect(timeout=tick_sec)
        else:
            stop.wait(tick_sec)
    scheduler.stop(accounts() if accounts is not None else None)
    save_now()
    return not lost


def _still_holds(conn: Any, timeout: float = 10.0) -> bool:
    """The lock's connection still answers (a database restart ends it, and
    the lock). One that doesn't answer within timeout (a dead connection can
    block for many minutes) counts as lost."""
    from sqlalchemy import text

    answered: list[bool] = []

    def ask() -> None:
        try:
            conn.execute(text("SELECT 1")).scalar()
            conn.commit()
            answered.append(True)
        except Exception:  # noqa: BLE001
            answered.append(False)

    asking = threading.Thread(target=ask, name="lock-check", daemon=True)
    asking.start()
    asking.join(timeout)
    return answered == [True]


def _hold_the_lock() -> Any:
    """Take the control-plane lock only one scheduler holds, on a connection
    kept open for the process's life; None when another scheduler has it."""
    from sqlalchemy import text

    from src.server.database import get_control_engine

    conn = get_control_engine().connect()
    if conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": LOCK_ID}).scalar():
        conn.commit()
        return conn
    conn.close()
    return None


def configure_logging() -> None:
    """INFO unless LOG_LEVEL says otherwise; never a line per HTTP request
    (httpx says each at INFO: several per clip)."""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger().setLevel(level)  # basicConfig leaves a configured root as it is
    for noisy in ("httpx", "httpcore", "pyvips"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    """lumiverb-scheduler: run every account's processing on this machine."""
    from src.client.cache_dir import cache_dir
    from src.client.cli.config import load_config
    from src.client.proxy.analysis_cache import clear_leftovers
    from src.server.scheduler.scans import (
        STATE_FILE,
        ServiceLock,
        load_state,
        save_state,
        saved_form,
    )

    configure_logging()
    lock = ServiceLock()
    if not lock.acquire():
        logger.error("scheduler: another scheduler (or the old worker) is running on this machine")
        return 1
    held = _hold_the_lock()
    if held is None:
        lock.release()
        logger.error("scheduler: another scheduler holds the lock in the control-plane database")
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
        cfg = load_config()
        gpu_decodes = cfg.gpu_decodes if cfg.analysis_proxy_decoder != "cpu" else 0
        scheduler = Scheduler(accounts, capacity=default_capacity(cfg), gpu_decodes=gpu_decodes)
        logger.info("scheduler: started")
        try:
            kept = run(stop=stop, scheduler=scheduler, save=save, accounts=accounts.all,
                       holds_lock=lambda: _still_holds(held))
        finally:
            accounts.close()
        logger.info("scheduler: stopped")
        return 0 if kept else 1
    finally:
        try:
            held.close()
        except Exception:  # noqa: BLE001 — a connection the database already ended
            pass
        lock.release()


def entry() -> None:
    """The program: main(), then out at once. Jobs still running past the
    grace save nothing more (and their threads would otherwise be waited
    on at exit, until systemd's kill)."""
    code = 1
    try:
        code = main()
    finally:
        logging.shutdown()
        os._exit(code)
