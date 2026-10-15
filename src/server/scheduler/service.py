"""The scheduler service: the server runs all processing (ADR-016 phase 4).

One process beside the API (systemd: lumiverb-scheduler), sharing its
database. Every second or so it:

1. has each account's queue topped up, off this thread (refill): its
   settings and AI machines read again when due, each library's storage
   looked at every 30 seconds (account.py look_at_storage: whatever is
   paused but Pause all), a new producer version's hold found, and each
   kind's due clips listed from its database (queue.py), less those in
   hand or just tried; each account's scan is offered every 30 seconds;
2. fills every free slot of every pool with the best job for it
   (dispatch.py: tier, then oldest first), run on a thread;
3. collects finished jobs.

Pools are sized as the producers declare them (src/producers/pools.py
for today's): so many slots, or this machine's setting, or each account's
AI machines (Settings → AI) times the job's per_request; a job whose
machines can't be used isn't handed out, and no clip is charged for it.
Jobs save their results through the API's routes on the server (runners.py,
src/producers/runner.py). The GPU models are the scheduler's, shared by
every account (models.py). A crash restarts the scheduler, never the site;
ffmpeg and face detection run in their own processes. One scheduler runs
at a time, on any machine (a lock in the control-plane database).
"""

from __future__ import annotations

import itertools
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from src.server.scheduler.dispatch import Dispatcher, Job, Outcome
from src.server.scheduler.kinds import AI_JOB_KINDS, KINDS, QUEUED
from src.server.scheduler.queue import BUFFER
from src.shared.stop_signals import set_on_signals

logger = logging.getLogger(__name__)

TICK_SEC = 1.0
# How long a stop waits for jobs in hand (which save nothing more) before
# leaving them to the next start.
STOP_GRACE_SEC = 25.0
# Failures are sent to the server at least this often.
FLUSH_EVERY_SEC = 10.0
# What the scheduler is doing is written for Settings → Processing this often.
STATUS_EVERY_SEC = 5.0
# What an admin paused (every switch, both scopes: lineage.pauses) is read
# this often, apart from the refill (which can be slow).
HOLD_EVERY_SEC = 1.0
# The control-plane lock only one scheduler holds ("lumv"), and how often
# it's checked that the lock is still held: before every tick hands out
# jobs (one query on its own connection), so a scheduler that lost it
# never runs beside the one that took it.
LOCK_ID = 0x6C756D76
LOCK_CHECK_SEC = 0.0
# Each account's new producer versions are looked for this often (and at
# its first refill, before any redo is listed).
NEW_VERSIONS_EVERY_SEC = 60.0

# Kinds whose AI job decides whether they're handed out.
_JOB_OF = {kind: job for job, kinds in AI_JOB_KINDS.items() for kind in kinds}


def default_capacity(here: Any) -> dict[str, int]:
    """The shared pools' slots, as each pool declares them: so many, or this
    machine's setting that sizes it (src/processing/machine.py). An account's
    own pools are its AI machines' (set as they're checked)."""
    from src.producers import pools

    return {p.name: (here.slots(p.sized_by) if p.sized_by else p.slots)
            for p in pools().values() if not p.per_account}


def account_pools() -> list[Any]:
    """Each account's own pools: its AI jobs' machines (Settings → AI)."""
    from src.producers import pools

    return [p for p in pools().values() if p.per_account]


def gpu_holds() -> dict[str, str]:
    """Pools whose jobs hold back the AI machines sharing this machine's GPU, and how."""
    from src.producers import pools

    return {p.name: p.gpu_hold for p in pools().values() if p.gpu_hold}


class Scheduler:
    def __init__(
        self,
        accounts: Callable[[], Mapping[str, Any]],
        *,
        capacity: Mapping[str, int],
        candidates: Callable[..., list[dict] | None] | None = None,
        on_hold: Callable[[str], Any] | None = None,
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
        crashed: Callable[[str, str, list[str], str], list[str]] | None = None,
        models: Any = None,
        new_versions: Callable[[str], Any] | None = None,
    ) -> None:
        from src.server.scheduler import runners as runner_mod

        self._accounts = accounts
        # The GPU models every account's jobs share (models.py): idle ones let go of.
        self._models = models
        self._candidates = candidates or _from_database
        self._on_hold = on_hold or _on_hold_in_database
        self._retry_requested = retry_requested or _retry_requested_in_database
        # Counts each clip's crashes in a row; returns those to charge now.
        self._crashed = crashed or _crashed_in_database
        # A producer's new version stops its redo until an admin resumes it
        # (lineage.hold_new_version): looked for in every producer made again.
        self._new_versions = new_versions or _hold_new_versions_in_database
        self._versions_at: dict[str, float] = {}
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
        # Each account's kinds whose last listing failed, and the error (its status says).
        self._unlisted: dict[str, dict[str, str]] = {}

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
        if self._models is not None:
            self._models.let_go_of_idle()
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
        """Video work comes first on this machine's GPU (Robert, Oct 9): the
        jobs of a pool that declares it (gpu_hold) take requests from the AI
        machines that share it, down to none: each decoding on the GPU (up to
        this machine's GPU decodes), or one while any runs."""
        from src.producers.contract import DECODES

        if self._gpu_decodes <= 0:
            hold = 0
        else:
            hold = sum(min(self._gpu_decodes if how == DECODES else 1, self.dispatcher.running(pool))
                       for pool, how in gpu_holds().items())
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

    def _read_hold(self, tenant_id: str) -> tuple[set[str], bool]:
        """What an admin paused, in one read (lineage.pauses): a switch's work
        (Scans, or a producer's: its kinds, and its redo's) and a producer's
        redo alone (its redo kind). The dispatcher hands out none of those
        kinds from now on; what's running finishes. Until it can be read, as
        if everything is paused. Returns the kinds held, and whether Pause
        all is on (or what's paused can't be read): then nothing touches
        the library drives, the storage look included."""
        seq = next(self._hold_reads)
        try:
            paused = self._on_hold(tenant_id)
            held, everything = held_kinds(paused), paused.everything
            if tenant_id in self._hold_unread:
                self._hold_unread.discard(tenant_id)
                logger.warning("scheduler: what %s paused can be read again", tenant_id)
        except Exception:  # noqa: BLE001
            if tenant_id not in self._hold_unread:  # once, not every second
                self._hold_unread.add(tenant_id)
                logger.exception("scheduler: reading what %s paused failed; nothing starts until it can be", tenant_id)
            held, everything = set(KINDS), True
        self.dispatcher.hold(tenant_id, held, seq=seq)
        return held, everything

    def _refill_soon(self, tenant_id: str, acct: Any) -> None:
        if self._inline_refill:
            self._refill(tenant_id, acct)
            return
        running = self._refilling.get(tenant_id)
        if running is None or running.done():
            self._refilling[tenant_id] = self._refill_pool.submit(self._refill, tenant_id, acct)

    def _refill(self, tenant_id: str, acct: Any) -> None:
        """Read the account's settings and machines when due, look at its
        libraries' storage and for new producer versions when due, size its
        AI pools, list what's due for each kind that wants more, and say
        what's running (every few seconds). What an admin paused (a switch,
        or a producer's redo) hands out nothing more, at once; what's
        running finishes.

        A pause stops its own work only. What other work depends on (whether
        a library's storage can be read, a new version's hold) is found out
        here, whatever is paused, never by a job: pausing Scans stops only
        looking for new and changed files. Pause all touches no library
        drive, so nothing looks at their storage then; on resuming, the
        look comes before any storage work is listed."""
        held, everything = self._read_hold(tenant_id)
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
        if not everything:
            # After the record from before a restart is in, so a library
            # unreachable now keeps when it was last seen.
            try:
                acct.look_at_storage()
            except Exception:  # noqa: BLE001 — looked at again at the next refill
                logger.exception("scheduler: looking at %s's storage failed", tenant_id)
        self._find_new_versions(tenant_id)
        if self._clock() - self._status_at.get(tenant_id, float("-inf")) >= STATUS_EVERY_SEC:
            self._status_at[tenant_id] = self._clock()
            try:
                # Libraries whose work on the originals waits: not reachable at
                # the latest look, or not looked at since this start.
                waits = sorted(set(acct.library_ids(storage=False)) - set(acct.library_ids(storage=True)))
                self._write_status(tenant_id, {**self.status(tenant_id), "storage_waits": waits,
                                               "storage": acct.storage_seen(), **self._why_nothing(tenant_id, acct)})
            except Exception:  # noqa: BLE001 — only a view of it
                logger.exception("scheduler: writing %s's status failed", tenant_id)
        for pool in account_pools():
            job = pool.job.name
            slots = acct.capacity(job)
            # As many jobs as the machines take requests, times the job's
            # per_request (transcripts: each clip's audio got ready while the
            # machines hear others; they hold their own limits).
            self.dispatcher.set_capacity(f"{pool.name}@{tenant_id}", slots * pool.job.per_request)
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
        if "scan" not in held and self.dispatcher.wanted(tenant_id, "scan"):
            self.dispatcher.offer(tenant_id, "scan", [{"asset_id": f"scan:{tenant_id}", "created_at": ""}],
                                  complete=False)
        if not acct.settings_ready:
            return  # nothing is made with settings the server hasn't said
        # Each gate below holds a kind's due work back, and Processing says why
        # (src/shared/waits.py): its AI machines (ai_waits), a pause, its
        # libraries' storage (storage_waits), a listing that failed (unlisted).
        unlisted = self._unlisted.setdefault(tenant_id, {})
        for kind in QUEUED:
            job = _JOB_OF.get(kind.name)
            if job is not None and not acct.capacity(job):
                unlisted.pop(kind.name, None)
                continue
            if kind.name in held:
                unlisted.pop(kind.name, None)
                continue
            if not self.dispatcher.wanted(tenant_id, kind.name):
                continue
            libraries = acct.library_ids(storage=kind.storage)
            if not libraries:  # nothing to ask yet (no storage reachable): not an empty answer to wait on
                unlisted.pop(kind.name, None)
                self.dispatcher.clear(tenant_id, kind.name)
                continue
            try:
                items = self._candidates(tenant_id, kind, libraries, self.dispatcher.held(tenant_id, kind.name))
            except Exception as e:  # noqa: BLE001 — one kind's trouble doesn't hold up the others
                logger.exception("scheduler: listing %s for %s failed", kind.name, tenant_id)
                unlisted[kind.name] = (str(e).strip().splitlines() or [type(e).__name__])[0][:300]
                items = []
            else:
                unlisted.pop(kind.name, None)
            if items is None:  # paused (or its redo stopped) since this refill looked: not an empty answer
                self.dispatcher.clear(tenant_id, kind.name)
                continue
            if kind.redo and items:
                acct.follow_settings(kind.artifact, items[0].get("settings_hash"))
            self.dispatcher.offer(tenant_id, kind.name, items, complete=len(items) < BUFFER)

    def _why_nothing(self, tenant_id: str, acct: Any) -> dict[str, Any]:
        """What its status says of work it hands out none of, for Processing
        to say why (src/shared/waits.py): whether it has read the account's
        settings (no job goes until then), each AI job whose machines it can't
        use and why (ai_waits), each kind whose listing failed (unlisted), and
        each library whose scans keep failing (scans_failing: tried again later)."""
        from src.server.scheduler.scans import failing_now

        return {
            "settings_ready": bool(acct.settings_ready),
            "ai_waits": {pool.job.name: acct.job_error(pool.job.name)
                         for pool in account_pools() if not acct.capacity(pool.job.name)},
            "unlisted": dict(self._unlisted.get(tenant_id, {})),
            "scans_failing": failing_now(getattr(acct, "scan_state", None), acct.library_ids(storage=False)),
        }

    def _find_new_versions(self, tenant_id: str) -> None:
        """A producer's new version stops its redo until an admin resumes it
        (lineage.hold_new_version): looked for in every producer made again,
        paused or not, at the account's first refill (before any redo is
        listed) and every NEW_VERSIONS_EVERY_SEC, so Processing shows it
        whatever can be listed. Only the database is asked, so Pause all
        doesn't stop it. Listing a redo looks too (_from_database)."""
        if self._clock() - self._versions_at.get(tenant_id, float("-inf")) < NEW_VERSIONS_EVERY_SEC:
            return
        try:
            self._new_versions(tenant_id)
        except Exception:  # noqa: BLE001 — tried again at the next refill
            logger.exception("scheduler: looking for %s's new producer versions failed", tenant_id)
            return
        self._versions_at[tenant_id] = self._clock()

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
        """The job's outcome. What escapes a runner is judged by whose it is
        (runners.whose): the clip's (the API refused what was made) is
        charged now; trouble reaching the API or its database charges
        nothing; anything else (a crash) charges nothing either, but each
        clip's crashes in a row are counted, and the CRASHES_BEFORE_CHARGE-th
        is charged, so the back-off and giving up apply."""
        from src.producers.runner import NOT_TRIED, Crashed, Stopped, whose
        from src.shared.waits import Waiting

        kind = KINDS[job.kind]
        try:
            if job.kind == "scan":
                self._scan(acct, job, now=self._wall())
                return None
            return self._runners[kind.base](acct, job)
        except Stopped:
            return NOT_TRIED
        except Exception as e:  # noqa: BLE001 — a job's surprise is logged; its clips are tried again later
            if not kind.flag:  # a scan's
                logger.exception("scheduler: %s for %s failed", job.kind, job.tenant_id)
                return list(job.asset_ids)
            # The clips that waited say why (a Waiting); the crashed ones are held (HELD).
            if isinstance(e, Crashed):
                crashed, others, cause = list(e.asset_ids), e.waiting, e.error
            else:
                crashed, others, cause = list(job.asset_ids), Waiting(), e
            names = ", ".join(crashed[:3])
            verdict = whose(cause)
            if verdict == "clip":
                # The API can't take what was made: the clip's failure, reported
                # (the server says when to try again), not made again every hour.
                logger.warning("scheduler: the server refused %s for %s: %s", job.kind, names, cause)
                for asset_id in crashed:
                    acct.failures.add(kind.artifact, asset_id, f"The server refused it: {cause}")
                return others or None
            if verdict == "transient":
                logger.warning("scheduler: %s for %s couldn't reach the server: %s", job.kind, names, cause)
                return Waiting(crashed + others, others.why)  # none charged: each waits the long while
            logger.error("scheduler: %s for %s crashed", job.kind, names, exc_info=cause)
            charged = self._charge_crashes(acct, job, crashed, cause)
            return Waiting([i for i in crashed + others if i not in charged], others.why) or None

    def _charge_crashes(self, acct: Any, job: Job, asset_ids: list[str], cause: object) -> set[str]:
        """Count one more crash in a row for each clip; charge a failure to
        those that reached CRASHES_BEFORE_CHARGE. Returns those charged."""
        from src.server.repository.lineage import CRASHES_BEFORE_CHARGE

        artifact = KINDS[job.kind].artifact
        error = str(cause).strip() or type(cause).__name__
        try:
            reached = self._crashed(job.tenant_id, artifact, asset_ids, error)
        except Exception:  # noqa: BLE001 — counted next time
            logger.exception("scheduler: counting %s's crashes for %s failed", job.kind, job.tenant_id)
            return set()
        for asset_id in reached:
            acct.failures.add(artifact, asset_id, f"Crashed {CRASHES_BEFORE_CHARGE} times in a row: {error}")
        return set(reached)

    def collect(self, timeout: float | None = 0) -> None:
        """Free the slots of finished jobs (waiting up to timeout for one)."""
        from src.producers.runner import NOT_TRIED

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


def stores_here() -> bool:
    """Whether the API keeps the previews on this machine's disk: the
    scheduler runs beside it, sharing its env (DATA_DIR, STORAGE_PROVIDER)."""
    from src.server.config import get_settings

    settings = get_settings()
    return settings.storage_provider == "local" and bool(settings.data_dir)


def forget_preview_copies() -> tuple[int, int] | None:
    """Where the API keeps the previews on this machine, the copies an
    earlier scheduler kept in its proxy cache (scans' and the faces step's)
    go: each clip's stored preview is read where it is (stored_preview).
    Only a cache inside the server's data folder (lumiverb-server run puts
    the scheduler's there): never a folder a CLI or the Mac app shares.
    Returns (files, bytes) removed, or None when there was nothing to do."""
    from src.processing.cache_dir import cache_dir
    from src.server.config import get_settings

    if not stores_here():
        return None
    folder = cache_dir("proxies").absolute()
    data = Path(get_settings().data_dir).absolute()
    if data not in folder.parents or not folder.is_dir():
        return None
    files = sizes = 0
    for entry in folder.rglob("*"):
        try:
            if entry.is_file():
                files, sizes = files + 1, sizes + entry.stat().st_size
        except OSError:
            continue
    shutil.rmtree(folder, ignore_errors=True)
    return files, sizes


def stored_analysis_proxy(tenant_id: str, storage: Any = None) -> Callable[[str], Any]:
    """The account's clip's analysis proxy as the API stored it, read where
    it is on this disk (src/server/storage/local.py: during a move, either
    folder): the scheduler's cache then holds no second copy. None when the
    clip has none, or its file isn't there (a drive unplugged): the cache
    downloads it then, as before."""
    return _stored_file(tenant_id, "analysis_proxy_key", storage)


def stored_preview(tenant_id: str, storage: Any = None) -> Callable[[str], Any]:
    """The account's clip's preview (its proxy) as the API stored it, read
    where it is on this disk, as stored_analysis_proxy: what CLIP, faces,
    OCR and descriptions see (src/processing/proxy/proxy_cache.py), with no
    copy in the scheduler's cache."""
    return _stored_file(tenant_id, "proxy_key", storage)


# The assets columns a stored file's key is read from.
_STORED_KEYS = {"analysis_proxy_key", "proxy_key"}


def _stored_file(tenant_id: str, column: str, storage: Any) -> Callable[[str], Any]:
    from sqlalchemy import text

    from src.server.database import get_tenant_session

    if column not in _STORED_KEYS:
        raise ValueError(column)

    def find(asset_id: str):
        nonlocal storage
        with get_tenant_session(tenant_id) as session:
            key = session.execute(text(f"SELECT {column} FROM assets WHERE asset_id = :a"),  # noqa: S608 — a known column
                                  {"a": asset_id}).scalar()
        if not key:
            return None
        if storage is None:
            from src.server.storage.local import get_storage

            storage = get_storage()
        path = storage.abs_path(key)
        return path if path.is_file() else None

    return find


def _from_database(tenant_id: str, kind: Any, libraries: list[str], skip: list[str] | None = None) -> list[dict] | None:
    """The kind's due clips, oldest first; None when it was paused (or its
    redo stopped) since the scheduler last looked."""
    from src.server.database import get_tenant_session
    from src.server.repository import lineage
    from src.server.scheduler.queue import candidates

    with get_tenant_session(tenant_id) as session:
        paused = lineage.pauses(session)
        if kind.artifact in paused.work:
            return None
        if not kind.redo:
            return candidates(session, kind, libraries, skip=skip)
        # A new producer version stops its redo until an admin resumes it
        # (each refill looks first, _find_new_versions; this is its guard).
        if kind.artifact in paused.redo or lineage.hold_new_version(session, kind.artifact):
            return None
        return candidates(session, kind, libraries, skip=skip,
                          want=lineage.desired(session, kind.artifact, _job_models(tenant_id)))


# The producers whose redo the scheduler lists (a redo kind each).
REDONE = tuple(dict.fromkeys(k.artifact for k in KINDS.values() if k.redo))


def _hold_new_versions_in_database(tenant_id: str) -> None:
    """lineage.hold_new_version for each producer made again, in the
    account's database (one read each once its version was seen)."""
    from src.server.database import get_tenant_session
    from src.server.repository import lineage

    with get_tenant_session(tenant_id) as session:
        for artifact in REDONE:
            lineage.hold_new_version(session, artifact)


def _job_models(tenant_id: str) -> dict[str, str]:
    """The account's model per AI job (Settings → AI)."""
    from src.server.repository.ai_machines import account_job_models

    return account_job_models(tenant_id)


def _retry_requested_in_database(tenant_id: str) -> str | None:
    """When someone last asked for failing clips to be tried again (lineage.retry)."""
    from sqlalchemy import text

    from src.server.database import get_tenant_session

    with get_tenant_session(tenant_id) as session:
        return session.execute(text("SELECT value FROM system_metadata WHERE key = 'scheduler.retry_at'")).scalar()


def _crashed_in_database(tenant_id: str, artifact: str, asset_ids: list[str], error: str) -> list[str]:
    """lineage.note_crashes in the account's database."""
    from src.server.database import get_tenant_session
    from src.server.repository import lineage

    with get_tenant_session(tenant_id) as session:
        return lineage.note_crashes(session, artifact, asset_ids, error)


def _status_to_database(tenant_id: str, status: dict) -> None:
    import json

    from sqlalchemy import text

    from src.server.database import get_tenant_session

    with get_tenant_session(tenant_id) as session:
        session.execute(text(
            "INSERT INTO system_metadata (key, value, updated_at) VALUES ('scheduler.status', :v, now())"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"), {"v": json.dumps(status)})
        session.commit()


def held_kinds(paused: Any) -> set[str]:
    """The kinds what's paused holds (lineage.Pauses): the scan when every
    library's scans are (Pause all, or each its own; a library paused alone
    is left out of the scan's pass, runners.scan); a producer's kinds for
    its switch; its redo kind for its redo alone."""
    held = set()
    for kind in KINDS.values():
        if not kind.flag:
            if kind.name == "scan" and paused.scans_paused():
                held.add(kind.name)
        elif kind.artifact in paused.work or (kind.redo and kind.artifact in paused.redo):
            held.add(kind.name)
    return held


def _on_hold_in_database(tenant_id: str) -> Any:
    """What an admin paused, both scopes (lineage.pauses)."""
    from src.server.database import get_tenant_session
    from src.server.repository import lineage

    with get_tenant_session(tenant_id) as session:
        return lineage.pauses(session)


def _status_from_database(tenant_id: str) -> dict | None:
    """The status the scheduler last wrote for the account (its paces, its storage looks)."""
    import json

    from sqlalchemy import text

    from src.server.database import get_tenant_session

    with get_tenant_session(tenant_id) as session:
        raw = session.execute(text("SELECT value FROM system_metadata WHERE key = 'scheduler.status'")).scalar()
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

KEY_LABEL = "scheduler"


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
    from src.processing.api import ApiClient

    class SchedulerClient(ApiClient):
        unauthorized = False

        def _handle_response(self, response):  # type: ignore[no-untyped-def]
            if response.status_code == 401:
                self.unauthorized = True
            return super()._handle_response(response)

    return SchedulerClient(url, key)


class Accounts:
    """Every active account, each with what its jobs run with; new accounts
    are picked up every few minutes, and one whose key stopped working is
    started again with a new one."""

    LIST_EVERY_SEC = 30.0  # a new account (first-run setup's) is worked for within half a minute
    # A listing that failed (the control plane away) is tried again sooner.
    LIST_RETRY_SEC = 30.0

    def __init__(self, scan_state: Any, *, url: str | None = None, models: Any = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._scan_state = scan_state
        self._models = models
        from src.server.scheduler.settings import api_url

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
                here = stores_here()
                self._accounts[tenant_id] = Account(tenant_id, client, self._scan_state, models=self._models,
                                                    stored=stored_analysis_proxy(tenant_id) if here else None,
                                                    stored_preview=stored_preview(tenant_id) if here else None)
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
    """The lock's connection still answers and holds the lock (a database
    restart ends both). One that doesn't answer within timeout (a dead
    connection can block for many minutes) counts as lost."""
    from sqlalchemy import text

    answered: list[bool] = []

    def ask() -> None:
        try:
            held = conn.execute(text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted"
                " AND pid = pg_backend_pid() AND classid = 0 AND objid = :id AND objsubid = 1)"),
                {"id": LOCK_ID}).scalar()
            conn.commit()
            answered.append(bool(held))
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
    from src.processing.cache_dir import cache_dir
    from src.processing import machine
    from src.processing.proxy.analysis_cache import clear_leftovers
    from src.server.scheduler.scans import (
        STATE_FILE,
        ServiceLock,
        load_state,
        save_state,
        saved_form,
    )

    from src.server.scheduler.settings import SettingError, read_settings

    from src.server.config import get_settings
    from src.server.database import set_application_name
    from src.shared import secrets_env

    configure_logging()
    set_application_name("lumiverb-scheduler")  # its connections, as PostgreSQL shows them
    try:
        settings = read_settings()
    except SettingError as exc:
        # Said here: entry()'s exit would otherwise end it with nothing said.
        logger.error("scheduler: not starting, a setting is wrong: %s", exc)
        return 1
    here = settings.machine()
    machine.use(here)  # never the CLI's config
    # Its settings read (and kept: get_settings), its secrets leave the
    # environment, so what it starts (ffmpeg, exiftool, its face and
    # transcript processes) has none.
    get_settings()
    secrets_env.forget()
    if removed := forget_preview_copies():
        logger.info("scheduler: removed %d files (%.1f GB) of preview copies from its cache: previews are read "
                    "where the API keeps them", removed[0], removed[1] / 1e9)
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
    set_on_signals(stop)  # SIGTERM, SIGINT
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

        from src.server.scheduler.models import Models

        models = Models(face_batches_per_process=here.face_batches_per_process)
        accounts = Accounts(state, models=models)
        scheduler = Scheduler(accounts, capacity=default_capacity(here), gpu_decodes=here.gpu_decodes_here,
                              models=models)
        logger.info("scheduler: started")
        try:
            kept = run(stop=stop, scheduler=scheduler, save=save, accounts=accounts.all,
                       holds_lock=lambda: _still_holds(held))
        finally:
            accounts.close()
            models.close()
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
    on at exit, until systemd's kill). Both ways in (python -m
    src.server.scheduler and the lumiverb-scheduler script) come here."""
    from src.shared.logging_config import escape_log_lines

    escape_log_lines()
    code = 1
    try:
        code = main()
    except BaseException:
        # Said here: os._exit below ends the process before Python would print it.
        logger.exception("scheduler: stopped by an error")
        raise
    finally:
        logging.shutdown()
        os._exit(code)
