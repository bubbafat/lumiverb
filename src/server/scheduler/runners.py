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
    (LUMIVERB_FULL_SCAN_EVERY_MIN) and when a scan of it was asked for. Each library's storage is looked at right
    before its scan, and what that finds goes into the account's record as
    it's found (on_roots), beside the scheduler's own looks (Account.look_at_storage)."""
    from src.server.scheduler.scans import scan_pass

    # Checked in first (ADR-018 phase 4): the libraries this server reads, and
    # those a scan of which was asked for (Scan now), as the server says now.
    acct.check_in()
    # Those whose scans aren't paused (ADR-018 phase 1): a paused one's reported changes wait.
    scanned = [lib for lib in acct.reading() if not lib.get("scans_paused")]

    def scanning(library_id: str | None) -> None:
        acct.scanning = library_id

    # Saved like a producer's work: no NUL (a file's EXIF can have one).
    scan_pass(Saving(acct.client), scanned, acct.scan_state, now=now,
              full_scan_every=acct.machine.full_scan_every_min * 60, on_roots=acct.set_reachable,
              scan_now=set(acct.scan_now), scanning=scanning, to_upload=acct.to_upload)


def runners() -> dict[str, Callable[[Account, Job], Outcome]]:
    """Each producer's work (its ``run``), by kind, loaded when first asked
    for: a producer's work module may import this one."""
    from src.producers import load
    from src.shared.producers import PRODUCERS

    return {p.artifact: partial(run, load(p.run)) for p in PRODUCERS.values() if p.scheduled}
