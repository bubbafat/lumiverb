"""One job of a producer's: the same steps for every producer (ADR-016 phase 4).

A producer supplies its work (src/producers/<artifact>/work.py): a Work
subclass that makes one clip's artifact (make) and saves what was made
(save). Everything else is here, once:

- Stopping. Once the scheduler is stopping nothing more is made or saved,
  and the job counts as not tried. Saves go through a client that refuses
  then (Stopped), so a write in hand can't slip through.
- Saving. What make returns is saved through the API right after (a
  producer whose clips are saved together, faces', once all are made). A
  make that's a generator saves each part as it comes (scene
  descriptions). Postgres takes no NUL in text, and a model's output can
  have one, so none is sent.
- Whose an error is. From make: the producer says (Failed: the clip's,
  charged now; Waits: nobody's, held the long while; NotTried: the job
  couldn't try at all), or judge() decides; the GPU running out of memory
  is nobody's; trouble with the API is judged by whose(); this machine's
  (OSError) and a process that died (Died) are crashes. From save: by
  whose(). The clip's is charged now (the server tries it again after 5
  minutes, doubling up to a day); the API or its database away charges
  nothing; anything else is a crash, uncharged but counted, and charged
  once the clip has crashed CRASHES_BEFORE_CHARGE times in a row
  (repository/lineage.py: the service counts them when the job raises
  Crashed). Two clips crashing in a row is the machine's trouble: the rest
  of the job waits.

What a job says (dispatch.Outcome): NOT_TRIED, None (each clip saved or
charged) or the clips that wait without either.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.server.scheduler.dispatch import Job

logger = logging.getLogger(__name__)

NOT_TRIED = "not_tried"

# A save the API refused for what's in it: the clip's failure, charged now.
REFUSED = frozenset({400, 413, 422})
# Not the clip's: who asked, the API or its database busy or away. Nothing
# is charged. (A 404 or 409 that keeps coming is counted, as a crash.)
NOT_THE_CLIPS = frozenset({401, 403, 408, 429, 502, 503, 504})


class Stopped(Exception):
    """The scheduler is stopping: nothing more is saved, and the job counts as not tried."""


class NotTried(Exception):
    """The job couldn't try at all (the storage went away): its clips aren't held back."""


class Failed(Exception):
    """The clip's failure, charged now with error."""

    def __init__(self, error: object) -> None:
        super().__init__(str(error))
        self.error = error


class Waits(Exception):
    """Nobody's fault (the GPU ran out of memory, the AI machines' trouble, the
    file changed since it was hashed): uncharged, the clip waits the long while."""


class Died(Exception):
    """A process the work ran in died or hung: a crash, uncharged but counted."""


class Crashed(Exception):
    """Some of a job's clips crashed: they're counted towards a charge; waiting aren't."""

    def __init__(self, asset_ids: list[str], error: object, waiting: list[str] | None = None) -> None:
        super().__init__(str(error))
        self.asset_ids = list(asset_ids)
        self.error = error
        self.waiting = list(waiting or [])


def whose(error: object) -> str:
    """Whose an error is: "clip" (charged now), "transient" (the API or its
    database: nothing charged) or "crash" (nothing charged, but counted)."""
    import httpx

    status = getattr(error, "status_code", None)
    if status in REFUSED:
        return "clip"
    if status in NOT_THE_CLIPS or isinstance(error, httpx.TransportError):
        return "transient"
    return "crash"


def api_error(error: object) -> bool:
    import httpx

    from src.processing.api import LumiverbAPIError

    return isinstance(error, (LumiverbAPIError, httpx.HTTPError))


def out_of_memory(error: object) -> bool:
    """The GPU's trouble, not the clip's: torch's and ONNX Runtime's words for it."""
    text = str(error).lower()
    return "out of memory" in text or "failed to allocate" in text


def strip_nul(value: Any) -> Any:
    """value with every NUL taken out of its strings (keys too), however deep."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {strip_nul(k): strip_nul(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [strip_nul(v) for v in value]
    return value


class Saving:
    """The account's client as work saves through it: no NUL in what's sent,
    and once the scheduler is stopping (when stopping is given), a write
    raises Stopped instead."""

    def __init__(self, client: Any, stopping: Any | None = None) -> None:
        # Not _client: what's passed through (a cache downloading) reads the client's own.
        self.__inner = client
        self.__stopping = stopping

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__inner, name)

    def _write(self, method: str, path: str, **kwargs: Any) -> Any:
        if self.__stopping is not None and self.__stopping.is_set():
            raise Stopped(path)
        if "json" in kwargs:
            kwargs = {**kwargs, "json": strip_nul(kwargs["json"])}
        return getattr(self.__inner, method)(path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self._write("post", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self._write("put", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        return self._write("patch", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self._write("delete", path, **kwargs)


class Work:
    """A producer's work for one job: make and save (and, when it needs to,
    judge and done). acct is what the job runs with (the scheduler's
    Account): its settings, machines, caches and models."""

    artifact = ""
    # Its clips are saved in one request once all of the job's are made (faces).
    together = False

    def __init__(self, acct: Any, job: Job) -> None:
        self.acct = acct
        self.job = job
        # Where make writes, for a producer that saves as it makes (scenes' chunks).
        self.client = Saving(acct.client, acct.stopping)

    def make(self, clip: dict) -> Any:
        """The clip's artifact as save takes it, or None when nothing is left
        to save (saved as it was made). Raise Failed, Waits or NotTried to
        say why nothing was made. A generator saves each part as it comes."""
        raise NotImplementedError

    def save(self, client: Saving, made: list[tuple[dict, Any]]) -> None:
        """Save what make made: (clip, what it returned) for each, through client."""
        raise NotImplementedError

    def judge(self, clip: dict, error: Exception) -> Exception:
        """Whose an error from make is that the rules above don't settle: Failed
        (the clip's: the default), Waits or NotTried."""
        return Failed(error)

    def done(self, clip: dict, made: Any) -> None:
        """What make made, saved or not: let go of what it holds (a file)."""

    # -- helpers for producers ------------------------------------------------

    def lineage(self, clip: dict | None, used: dict[str, Any] | None = None) -> dict:
        """What a save records: this producer, its version, the settings used
        (the server's unless used says) and the clip's file."""
        return self.acct.producers.lineage(self.artifact, clip.get("sha256") if clip else None, used=used)

    def machines_or_clip(self, error: object) -> Exception:
        """A failure of the producer's AI job: the clip's (Failed) when its
        machines' guard says so (src/processing/job_guard.py), else theirs (Waits)."""
        from src.shared.producers import PRODUCERS

        guard = self.acct.guard(PRODUCERS[self.artifact].job)
        return Failed(error) if guard.charges(error) else Waits(error)

    def original(self, clip: dict) -> Any:
        """The clip's original on the library's storage. The storage away at
        the last look, or gone since: the job isn't tried (NotTried). The file
        missing from storage that's there waits for the scan that says so."""
        from src.shared.io_utils import resolve_source_path

        acct, library_id = self.acct, clip["library_id"]
        root = acct.root(library_id)
        if root is None:
            logger.info("scheduler: %s's storage isn't reachable; its storage work waits", clip["rel_path"])
            acct.unreachable(library_id)
            raise NotTried(clip["rel_path"])
        source = resolve_source_path(root, clip["rel_path"])
        if not source.is_file():
            if acct.storage_gone(library_id):
                logger.info("scheduler: %s's storage went away; its storage work waits", clip["rel_path"])
                acct.unreachable(library_id)
                raise NotTried(clip["rel_path"])
            logger.warning("scheduler: %s isn't on its storage; it waits for the scan", clip["rel_path"])
            raise Waits("missing")
        return source


class _Job:
    """One run of a producer's work over a job's clips."""

    def __init__(self, work: Work, acct: Any, job: Job) -> None:
        self.work, self.acct, self.job = work, acct, job
        self.saving = Saving(acct.client, acct.stopping)
        self.waiting: list[str] = []
        self.crashed: list[str] = []
        self.cause: object = None
        self.in_a_row = 0  # clips crashed in a row

    # -- outcomes for one clip ----------------------------------------------

    def charge(self, clip: dict, error: object) -> None:
        self.in_a_row = 0
        self.acct.failures.add(self.work.artifact, clip["asset_id"], error)

    def wait(self, clip: dict) -> None:
        self.in_a_row = 0
        self.waiting.append(clip["asset_id"])

    def crash(self, clip: dict, error: object) -> None:
        logger.error("scheduler: %s for %s crashed", self.work.artifact, clip["rel_path"], exc_info=error
                     if isinstance(error, BaseException) else None)
        self.in_a_row += 1
        self.crashed.append(clip["asset_id"])
        self.cause = error

    def made(self, clip: dict) -> None:
        self.in_a_row = 0

    def judged(self, clip: dict, error: Exception) -> None:
        """An error from make: whose it is, settled."""
        if isinstance(error, (Stopped, NotTried)):
            raise error
        if isinstance(error, Failed):
            self.charge(clip, error.error)
        elif isinstance(error, Waits):
            logger.info("scheduler: %s for %s waits: %s", self.work.artifact, clip["rel_path"], error)
            self.wait(clip)
        elif isinstance(error, Died) or isinstance(error, OSError):
            self.crash(clip, error)
        elif out_of_memory(error):
            logger.warning("scheduler: the GPU ran out of memory for %s; %s waits", self.work.artifact,
                           clip["rel_path"])
            self.wait(clip)
        elif api_error(error):
            self.saved_badly(clip, error)
        else:
            verdict = self.work.judge(clip, error)
            if isinstance(verdict, (Failed, Waits, NotTried, Stopped, Died)):
                self.judged(clip, verdict)
            else:  # a judge that hands back something else: the clip's
                self.charge(clip, verdict)

    def saved_badly(self, clip: dict, error: object) -> None:
        """An error from the API: whose() it is."""
        verdict = whose(error)
        if verdict == "clip":
            logger.warning("scheduler: the server refused %s for %s: %s", self.work.artifact, clip["rel_path"], error)
            self.charge(clip, f"The server refused it: {error}")
        elif verdict == "transient":
            logger.warning("scheduler: %s for %s couldn't reach the server: %s", self.work.artifact,
                           clip["rel_path"], error)
            self.wait(clip)
        else:
            self.crash(clip, error)

    # -- saving -------------------------------------------------------------

    def save(self, made: list[tuple[dict, Any]]) -> bool:
        """Save what was made; False when a save failed (each clip's outcome is
        settled). A refusal of several clips together is tried one by one, so
        only the clip the API refuses is charged."""
        if self.acct.stopping.is_set():
            raise Stopped(self.work.artifact)
        try:
            self.work.save(self.saving, made)
        except Stopped:
            raise
        except Exception as e:  # noqa: BLE001 — whose it is, below
            if len(made) > 1 and api_error(e) and whose(e) == "clip":
                ok = True
                for one in made:
                    ok = self.save([one]) and ok
                return ok
            for clip, _ in made:
                if api_error(e):
                    self.saved_badly(clip, e)
                else:
                    self.crash(clip, e)
            return False
        for clip, _ in made:
            self.made(clip)
        return True

    # -- the job ------------------------------------------------------------

    def run(self) -> Any:
        work, together = self.work, self.work.together
        kept: list[tuple[dict, Any]] = []
        items = list(self.job.items)
        try:
            for n, clip in enumerate(items):
                if self.acct.stopping.is_set():
                    return NOT_TRIED
                if self.in_a_row >= 2:
                    # Two in a row crashing is the machine's trouble (a hung GPU): the rest wait.
                    logger.warning("scheduler: %s keeps crashing; the rest of the job waits", work.artifact)
                    self.waiting.extend(c["asset_id"] for c in items[n:])
                    break
                self.one(clip, kept if together else None)
            if kept:
                try:
                    self.save(kept)
                finally:
                    for clip, made in kept:
                        work.done(clip, made)
        except (Stopped, NotTried):
            for clip, made in kept:
                work.done(clip, made)
            return NOT_TRIED
        if self.crashed:
            # Uncharged now; the service counts each clip's crashes in a row.
            raise Crashed(self.crashed, self.cause, waiting=self.waiting)
        return self.waiting or None

    def one(self, clip: dict, kept: list[tuple[dict, Any]] | None) -> None:
        """Make the clip's artifact and save it (or keep it, to save together)."""
        work = self.work
        try:
            made = work.make(clip)
            if inspect.isgenerator(made):
                self.parts(clip, made)
                return
        except Exception as e:  # noqa: BLE001 — whose it is, in judged()
            self.judged(clip, e)
            return
        if made is None:  # saved as it was made
            self.made(clip)
            return
        if kept is not None:
            kept.append((clip, made))
            return
        try:
            self.save([(clip, made)])
        finally:
            work.done(clip, made)

    def parts(self, clip: dict, parts: Iterator[Any]) -> None:
        """A clip made in parts: each saved as it comes. One that fails to save
        settles the clip, and nothing more of it is made."""
        while True:
            try:
                part = next(parts)
            except StopIteration:
                self.made(clip)
                return
            except Exception as e:  # noqa: BLE001
                self.judged(clip, e)
                return
            try:
                saved = self.save([(clip, part)])
            finally:
                self.work.done(clip, part)
            if not saved:
                parts.close()
                return


def run(work_class: type[Work], acct: Any, job: Job) -> Any:
    """The job's outcome (see the module's docstring)."""
    if acct.stopping.is_set():
        return NOT_TRIED
    try:
        work = work_class(acct, job)
    except (Stopped, NotTried):
        return NOT_TRIED
    return _Job(work, acct, job).run()
