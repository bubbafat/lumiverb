"""This machine as the reader of libraries on a server elsewhere: a device
(ADR-018 phase 6, decision 2). `lumiverb device run` runs it, with the
device's key (Devices API) as the CLI's API key.

It does for the libraries it reads what this server's scheduler does for
its own, through the same code, and nothing else:

1. **Checks in** every minute (POST /v1/readers/check-in), a pass's long
   scan or renders too: each library's state, what its latest scan there
   has left to send (files and their size on disk), its last full scan and
   the mark it last fetched at; the answer is the libraries it reads. When
   a library's mark moved, it fetches what changed (reader-changes): what
   each thing it makes is made with now, and what of that is paused.
2. **Looks at each library's storage**, then **scans** as the scheduler
   does (scan_pass: reported changes, a full scan every full_scan_every
   seconds, Scan now, a failing file's back-off; scans paused, not
   scanned). A scan adds new and changed files with their previews first.
3. **Makes what the server lists as needing the originals** (reader-work,
   a page at a time: probe facts, capture, analysis proxies, in that
   order, so renders come last; then what's made another way than now, the
   remakes, unless they're paused), through each producer's own work and
   the one runner (src/producers/runner.py), as the scheduler's jobs are;
   what it couldn't make is reported (POST /v1/producers/failures) and so
   leaves the list until its back-off ends.

Remakes (decision 6) come after everything else: they stop at the next
clip when new work may have come (a scan due, Scan now, the server's mark
moved), the next pass taking it first, and wait while this machine is on
battery or a metered network (src/processing/power.py), its scans then
asking to remake no preview; its check-ins say so (remakes_held).

It keeps no copy it decides from (decision 13): what there is to do is the
server's list, asked for again every pass, and a scan compares the disk
with the server, never with a record of its own. What it keeps between
passes (its marks, a failing file's back-off) it can lose: a restart starts
from mark 0, which is everything.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

from src.producers import names

logger = logging.getLogger(__name__)

# What a device makes from the originals, in the order it makes them: renders last.
ORDER = (names.VIDEO_FORMAT, names.CAPTURE_TIME, names.LIVE_PHOTO, names.ANALYSIS_PROXY)
PAGE = 500  # reader-work's clips a page


def _ids(words: list[str] | None) -> set[str]:
    """What the server lists (what's paused, a clip's work), by name."""
    return set(words or [])


class Device:
    """A device's reading of its libraries: what a producer's work asks of
    its account (client, stopping, inputs, producers, failures, machine),
    and the passes."""

    def __init__(self, client: Any, *, full_scan_every: float, name: str | None = None,
                 clock: Callable[[], float] = time.time, stopping: threading.Event | None = None,
                 held: Callable[[], str | None] | None = None) -> None:
        from src.processing.failure_report import FailureReport
        from src.processing.machine import current
        from src.processing.producer_settings import ProducerSettings
        from src.server.scheduler.inputs import Inputs
        from src.server.scheduler.scans import ScanState

        self.tenant_id = "device"  # a job's account: whichever the key's is
        self.client = client
        self.stopping = stopping or threading.Event()
        self.machine = current()  # this machine's encoder and decoder (the CLI's settings)
        self.producers = ProducerSettings(None, fetch=False)  # as reader-changes tells it
        self.failures = FailureReport(client)
        self.inputs = Inputs(self, None, keep_renders=False)  # it makes nothing from its renders
        self.scan_state = ScanState()
        self.full_scan_every = full_scan_every
        self.name = name or socket.gethostname()
        self.check_in_every = 60.0
        self._clock = clock
        self.libraries: dict[str, dict] = {}  # those it reads, as its latest check-in heard them
        self.marks: dict[str, int] = {}  # each one's mark, as last fetched
        self.told: dict[str, dict] = {}  # each one's reader-changes
        self.reached: dict[str, Any] = {}  # each one's originals here at the latest look (None: not here)
        self.to_send: dict[str, tuple[int, int]] = {}  # what each one's latest scan has left to send
        self.scanning: str | None = None
        # Why its remakes wait now (power.held: "battery", "metered"), as its check-ins say; None: they go on.
        self.remakes_held: str | None = None
        self._held = held or _held
        self.checked_in_at: float | None = None
        self._checking_in = threading.Lock()  # one check-in at a time: a pass's, or one during a long pass

    # -- What a producer's inputs ask of it (src/server/scheduler/inputs.py) -------------

    def originals(self, library_id: str) -> Any:
        return self.reached.get(library_id)

    def unreachable(self, library_id: str) -> None:
        self.reached[library_id] = None

    def storage_gone(self, library_id: str) -> bool:
        from src.processing.originals import reach

        library = self.libraries.get(library_id)
        return library is None or reach(library, require_entries=True) is None

    def guard(self, job: str) -> Any:
        raise RuntimeError(f"a device runs no AI job ({job}): its server does")

    # -- A pass -------------------------------------------------------------------

    def check_in(self) -> list[str]:
        """Check in (decision 4): the libraries it reads now, and what
        changed in those whose mark moved. Returns those it no longer reads."""
        from src.server.check_ins import waiting
        from src.server.version import running
        from src.shared.readers import PROTOCOL

        with self._checking_in:
            self.checked_in_at = self._clock()  # tried: one that fails is tried again in a minute
            held = {"remakes_held": self.remakes_held} if self.remakes_held else {}
            said = [{"library_id": i, "state": self._state(lib), "mark": self.marks.get(i, 0),
                     **waiting(self.to_send.get(i)), **held,
                     "last_full_scan_at": _iso(self.scan_state.last_full_scan.get(i))}
                    for i, lib in self.libraries.items()]
            answer = self.client.post("/v1/readers/check-in", json={
                "client": self.name, "version": running(), "protocol": PROTOCOL, "libraries": said},
                retry=True).json()
            self.check_in_every = float(answer.get("check_in_every_sec") or self.check_in_every)
            self.libraries = {lib["library_id"]: lib for lib in answer["libraries"]}
            for library_id in {*self.marks, *self.to_send}:
                if library_id not in self.libraries:  # gone: another reader's now, or in the trash
                    self.marks.pop(library_id, None)
                    self.told.pop(library_id, None)
                    self.reached.pop(library_id, None)
                    self.to_send.pop(library_id, None)
            for library_id, library in self.libraries.items():
                # While a producer's settings are refused (told), asked afresh at each check-in: a change on
                # the way needn't last, and the mark moves only when the server changes something.
                if library_id not in self.told or self.marks.get(library_id) != library["mark"] \
                        or self.producers.refused:
                    changes = self.client.get(f"/v1/libraries/{library_id}/reader-changes",
                                              params={"since": self.marks.get(library_id, 0)}).json()
                    self.told[library_id] = changes
                    self.producers.told(changes.get("made_with") or {})
                    self.marks[library_id] = changes["mark"]
            return list(answer.get("gone") or [])

    def check_in_if_due(self) -> bool:
        """Check in when it hasn't for check_in_every seconds: during a long
        scan or render, so the server hears from it every minute as between
        passes (a reader not heard from for 5 minutes is said to be). True
        when it checked in."""
        if self.checked_in_at is not None and self._clock() - self.checked_in_at < self.check_in_every:
            return False
        self.check_in()
        return True

    def to_upload(self, library_id: str, files: int, size: int) -> None:
        """What the library's scan has found to add or read again and not
        sent yet (the scan pass says, as it goes): its check-ins say it until
        its next scan says otherwise, as a scan stopped part way leaves them
        to send."""
        self.to_send[library_id] = (files, size)

    def _state(self, library: dict) -> str:
        library_id = library["library_id"]
        if self.scanning == library_id:
            return "scanning"
        if library.get("scans_paused"):
            return "paused"
        if library_id in self.reached and self.reached[library_id] is None:
            return "away"
        return "idle"

    def look(self) -> None:
        """Each library's storage here, as a scan looks at it (an unmounted
        mount point is an empty folder)."""
        from src.processing.originals import reach

        self.reached = {i: reach(lib, require_entries=True) for i, lib in self.libraries.items()}

    def scan(self) -> None:
        """Scan what's due in each library whose scans aren't paused, as the scheduler does."""
        from src.producers.runner import Saving
        from src.server.scheduler.scans import scan_pass

        def scanning(library_id: str | None) -> None:
            self.scanning = library_id

        scan_pass(Saving(self.client, self.stopping),
                  [lib for lib in self.libraries.values() if not lib.get("scans_paused")], self.scan_state,
                  now=self._clock(), full_scan_every=self.full_scan_every, on_roots=self.reached.update,
                  scan_now={i for i, lib in self.libraries.items() if lib.get("scan_now")},
                  scanning=scanning, cache_previews=False, to_upload=self.to_upload,
                  remakes=self.remakes_held is None)

    def work(self) -> int:
        """What the server lists as needing the originals, in each library
        reachable here: made, saved, or reported as failing. The remakes
        after everything else, while they go on (_Remaking). How many clips
        it went through."""
        done = 0
        remaking = _Remaking(self)
        for field in ("work", "remakes"):
            if field == "remakes" and remaking.held():
                break
            for library_id in list(self.libraries):
                if self.reached.get(library_id) is None:
                    continue
                paused = (self.told.get(library_id) or {}).get("paused") or {}
                held = _ids(paused.get("work")) | (_ids(paused.get("remakes")) if field == "remakes" else set())
                for page in self._pages(library_id):
                    for artifact in ORDER:
                        if self.stopping.is_set():
                            return done
                        if artifact in held or artifact in self.producers.refused:  # refused: said in the log
                            continue
                        clips = [c for c in page if artifact in _ids(c.get(field))]
                        done += self._make(library_id, artifact, clips,
                                           stop=remaking.stop if field == "remakes" else None)
                        if remaking.stopped:
                            self.failures.flush()
                            return done
        self.failures.flush()
        return done

    def scan_due(self) -> bool:
        """A scan of a library it reads is due: a full scan's time has come,
        or one was asked for (Scan now). Scans paused: none is."""
        now = self._clock()
        return any(not lib.get("scans_paused")
                   and (lib.get("scan_now")
                        or now - self.scan_state.last_full_scan.get(i, float("-inf")) >= self.full_scan_every)
                   for i, lib in self.libraries.items())

    def _pages(self, library_id: str) -> Iterator[list[dict]]:
        after = None
        while not self.stopping.is_set():
            params = {"limit": PAGE, **({"after": after} if after else {})}
            page = self.client.get(f"/v1/libraries/{library_id}/reader-work", params=params).json()
            if page["clips"]:
                yield page["clips"]
            after = page.get("next_cursor")
            if not after:
                return

    def _make(self, library_id: str, artifact: str, clips: list[dict],
              stop: Callable[[], bool] | None = None) -> int:
        """The clips' artifact, made through the producer's work and the
        runner, in jobs of the producer's batch; stop, asked before each
        job, ends it early (a remake yielding)."""
        from src.producers import load
        from src.producers.runner import run
        from src.server.scheduler.dispatch import Job
        from src.shared.producers import PRODUCERS

        spec = PRODUCERS[artifact]
        batch = max(1, int(getattr(spec, "batch", 1) or 1))
        for i in range(0, len(clips), batch):
            if self.stopping.is_set() or (stop is not None and stop()):
                return i
            items = tuple({**c, "library_id": library_id} for c in clips[i:i + batch])
            run(load(spec.run), self, Job(self.tenant_id, spec.artifact, 0, items))
            self.failures.flush()
        return len(clips)

    def once(self) -> int:
        """One pass: check in, look, scan, then the work on the originals.
        How many clips the work went through."""
        self.remakes_held = self._held()  # its check-ins and scans go by it
        self.check_in()
        self.look()
        self.scan()
        if self.stopping.is_set():
            return 0
        self.check_in()  # what the scan added is listed now; say it scanned
        return self.work()

    def run(self, *, say: Callable[[str], None] = logger.info) -> None:
        """Pass after pass until stopped: checking in at least every
        check_in_every seconds between them, and during a long one
        (keep_checking_in)."""
        beat = threading.Thread(target=self.keep_checking_in, name="device-check-in", daemon=True)
        beat.start()
        try:
            while not self.stopping.is_set():
                started = self._clock()
                try:
                    done = self.once()
                    if done:
                        say(f"Went through {done} clip(s)")
                except Exception:  # noqa: BLE001 — the server away, a drive gone: the next pass tries again
                    logger.exception("device: a pass failed; trying again")
                self.stopping.wait(max(0.0, self.check_in_every - (self._clock() - started)))
        finally:
            self.stopping.set()
            beat.join(timeout=5)

    def keep_checking_in(self, every: float = 5.0) -> None:
        """Until stopped, check in whenever check_in_every seconds passed
        since the last check-in (check_in_if_due), looking every `every`
        seconds: a pass checks in at its start, but a scan of a big library's
        new files or a pass's renders can take hours."""
        while not self.stopping.wait(every):
            try:
                self.check_in_if_due()
            except Exception as exc:  # noqa: BLE001 — the server away: tried again at the next look
                logger.warning("device: couldn't check in (%s); trying again", exc)


class _Remaking:
    """When a pass's remakes stop (ADR-018 decision 6): at once while the
    device holds them back (on battery, a metered network), and, once one
    job is made, when new work may have come since the pass's check-in (a
    scan due or asked for, the server's mark moved): the next pass takes it
    first. Asked before each job."""

    def __init__(self, device: Device) -> None:
        self.device = device
        self.marks = dict(device.marks)
        self.jobs = 0
        self.stopped = False

    def held(self) -> bool:
        """Whether they wait now; the device's check-ins say why."""
        self.device.remakes_held = self.device._held()
        self.stopped = self.stopped or self.device.remakes_held is not None
        return self.stopped

    def stop(self) -> bool:
        if not self.held() and self.jobs and (self.device.marks != self.marks or self.device.scan_due()):
            self.stopped = True
        self.jobs += 1
        return self.stopped


def _held() -> str | None:
    from src.processing.power import held

    return held()


def _iso(when: float | None) -> str | None:
    return datetime.fromtimestamp(when, UTC).isoformat() if when is not None else None
