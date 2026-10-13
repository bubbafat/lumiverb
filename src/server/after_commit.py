"""What can't be undone with the database, held until the transaction that
made it so has committed.

Archiving clips and handing them over to their copies commits as one
transaction (a scan's finish, DELETE /v1/assets): all of it, or none of it
when the API dies halfway. What goes with it outside the database (a deleted
copy's files, its playback cuts and search documents, a transcript indexed
again, a grid's revision) can't be rolled back, so it waits here until the
caller has committed, then runs. Rolled back, it never runs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)


class AfterCommit:
    """Steps to run once the caller has committed, in order. Each on its own:
    one that fails is logged and the rest still run (the database is right;
    these only follow it)."""

    def __init__(self) -> None:
        self._steps: list[tuple[str, Callable[[], None]]] = []

    def add(self, what: str, step: Callable[[], None]) -> None:
        """what: said in the log if the step fails ("Removing a copy's files")."""
        self._steps.append((what, step))

    def extend(self, other: AfterCommit) -> None:
        """Another's steps after these (a savepoint's, once it's kept)."""
        self._steps.extend(other._steps)
        other._steps = []

    def run(self) -> None:
        steps, self._steps = self._steps, []
        for what, step in steps:
            try:
                step()
            except Exception as exc:  # noqa: BLE001 — what it cleans up after has committed
                logger.warning("%s failed after the commit: %s", what, exc)

    def __len__(self) -> int:
        return len(self._steps)
