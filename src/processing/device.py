"""This machine as the reader of libraries on a server elsewhere: a device
(ADR-018 phase 6, decision 2). `lumiverb device run` runs it, with the
device's key (Devices API) as the CLI's API key.

It does for the libraries it reads what this server's scheduler does for
its own, through the same code, and nothing else:

1. **Checks in** every minute (POST /v1/readers/check-in): each library's
   state, its last full scan and the mark it last fetched at; the answer is
   the libraries it reads. When a library's mark moved, it fetches what
   changed (reader-changes): what each thing it makes is made with now, and
   what of that is paused.
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

logger = logging.getLogger(__name__)

# What a device makes from the originals, in the order it makes them: renders last.
ORDER = ("probe", "capture", "analysis_proxy")
PAGE = 500  # reader-work's clips a page


class Device:
    """A device's reading of its libraries: what a producer's work asks of
    its account (client, stopping, inputs, producers, failures, machine),
    and the passes."""

    def __init__(self, client: Any, *, full_scan_every: float, name: str | None = None,
                 clock: Callable[[], float] = time.time, stopping: threading.Event | None = None) -> None:
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
        self.scanning: str | None = None

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
        from src.server.check_ins import PROTOCOL
        from src.server.version import running

        said = [{"library_id": i, "state": self._state(lib), "mark": self.marks.get(i, 0),
                 "last_full_scan_at": _iso(self.scan_state.last_full_scan.get(i))}
                for i, lib in self.libraries.items()]
        answer = self.client.post("/v1/readers/check-in", json={
            "client": self.name, "version": running(), "protocol": PROTOCOL, "libraries": said}, retry=True).json()
        self.check_in_every = float(answer.get("check_in_every_sec") or self.check_in_every)
        self.libraries = {lib["library_id"]: lib for lib in answer["libraries"]}
        for library_id in list(self.marks):
            if library_id not in self.libraries:  # gone: another reader's now, or in the trash
                self.marks.pop(library_id, None)
                self.told.pop(library_id, None)
                self.reached.pop(library_id, None)
        for library_id, library in self.libraries.items():
            if library_id not in self.told or self.marks.get(library_id) != library["mark"]:
                changes = self.client.get(f"/v1/libraries/{library_id}/reader-changes",
                                          params={"since": self.marks.get(library_id, 0)}).json()
                self.told[library_id] = changes
                self.producers.told(changes.get("made_with") or {})
                self.marks[library_id] = changes["mark"]
        return list(answer.get("gone") or [])

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
                  scanning=scanning, cache_previews=False)

    def work(self) -> int:
        """What the server lists as needing the originals, in each library
        reachable here: made, saved, or reported as failing. The remakes
        after everything else. How many clips it went through."""
        done = 0
        for field in ("work", "remakes"):
            for library_id in list(self.libraries):
                if self.reached.get(library_id) is None:
                    continue
                paused = (self.told.get(library_id) or {}).get("paused") or {}
                held = set(paused.get("work") or []) | (set(paused.get("remakes") or []) if field == "remakes"
                                                         else set())
                for page in self._pages(library_id):
                    for artifact in ORDER:
                        if self.stopping.is_set():
                            return done
                        if artifact in held:
                            continue
                        clips = [c for c in page if artifact in (c.get(field) or [])]
                        done += self._make(library_id, artifact, clips)
        self.failures.flush()
        return done

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

    def _make(self, library_id: str, artifact: str, clips: list[dict]) -> int:
        """The clips' artifact, made through the producer's work and the
        runner, in jobs of the producer's batch."""
        from src.producers import load
        from src.producers.runner import run
        from src.server.scheduler.dispatch import Job
        from src.shared.producers import PRODUCERS

        spec = PRODUCERS[artifact]
        batch = max(1, int(getattr(spec, "batch", 1) or 1))
        for i in range(0, len(clips), batch):
            if self.stopping.is_set():
                return i
            items = tuple({**c, "library_id": library_id} for c in clips[i:i + batch])
            run(load(spec.run), self, Job(self.tenant_id, spec.kind, 0, items))
            self.failures.flush()
        return len(clips)

    def once(self) -> int:
        """One pass: check in, look, scan, then the work on the originals.
        How many clips the work went through."""
        self.check_in()
        self.look()
        self.scan()
        if self.stopping.is_set():
            return 0
        self.check_in()  # what the scan added is listed now; say it scanned
        return self.work()

    def run(self, *, say: Callable[[str], None] = logger.info) -> None:
        """Pass after pass until stopped: checking in at least every
        check_in_every seconds between them."""
        while not self.stopping.is_set():
            started = self._clock()
            try:
                done = self.once()
                if done:
                    say(f"Went through {done} clip(s)")
            except Exception:  # noqa: BLE001 — the server away, a drive gone: the next pass tries again
                logger.exception("device: a pass failed; trying again")
            self.stopping.wait(max(0.0, self.check_in_every - (self._clock() - started)))


def _iso(when: float | None) -> str | None:
    return datetime.fromtimestamp(when, UTC).isoformat() if when is not None else None
