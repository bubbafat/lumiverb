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
    """Look at the account's libraries: scan what's due, and note where each one's storage is."""
    from src.server.scheduler.scans import scan_pass

    libraries = acct.client.get("/v1/libraries").json()
    acct.set_libraries(libraries)
    # Saved like a producer's work: no NUL (a file's EXIF can have one).
    acct.set_reachable(scan_pass(Saving(acct.client), libraries, acct.scan_state, now=now,
                                 on_roots=acct.set_reachable))


def runners() -> dict[str, Callable[[Account, Job], Outcome]]:
    """Each producer's work (its ``run``), by kind, loaded when first asked
    for: a producer's work module may import this one."""
    from src.producers import load
    from src.shared.producers import PRODUCERS

    return {p.kind: partial(run, load(p.run)) for p in PRODUCERS.values() if p.scheduled}
