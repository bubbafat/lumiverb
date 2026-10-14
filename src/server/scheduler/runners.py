"""What the scheduler runs for a job (ADR-016 phase 4): a producer's work,
through the one runner every producer's goes through (src/producers/runner.py:
stopping, saving, whose an error is), or the scan pass.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING

from src.producers.runner import Saving, run
from src.server.scheduler.dispatch import Job, Outcome

if TYPE_CHECKING:
    from src.server.scheduler.account import Account


def scan(acct: Account, job: Job, *, now: float) -> None:
    """Look for new and changed files in the account's libraries: scan what's
    due, each library in full as often as this machine's setting says
    (LUMIVERB_FULL_SCAN_EVERY_MIN). Each library's storage is looked at right
    before its scan, and what that finds goes into the account's record as
    it's found (on_roots), beside the scheduler's own looks (Account.look_at_storage)."""
    from src.server.scheduler.scans import scan_pass
    from src.shared.readers import read_by_server

    libraries = acct.client.get("/v1/libraries").json()
    acct.set_libraries(libraries)
    # This server's libraries (src/shared/readers.py) whose scans aren't
    # paused (ADR-018 phase 1): a paused one's reported changes wait.
    scanned = [lib for lib in libraries if read_by_server(lib) and not lib.get("scans_paused")]
    # Saved like a producer's work: no NUL (a file's EXIF can have one).
    scan_pass(Saving(acct.client), scanned, acct.scan_state, now=now,
              full_scan_every=acct.machine.full_scan_every_min * 60, on_roots=acct.set_reachable)


def runners() -> dict[str, Callable[[Account, Job], Outcome]]:
    """Each producer's work (its ``run``), by kind, loaded when first asked
    for: a producer's work module may import this one."""
    from src.producers import load
    from src.shared.producers import PRODUCERS

    return {p.kind: partial(run, load(p.run)) for p in PRODUCERS.values() if p.scheduled}
