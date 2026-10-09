"""A job's AI work only while a machine doing it offers the account's model (ADR-016 phase 3).

Before a job's steps, the worker checks each of the account's machines
doing it (Settings → AI) and tells the server what each said; Settings
shows it. Requests go to the online machines (src/client/cli/ai_pool.py);
one that fails is skipped and its item goes to another. When no machine can
be used, the job's work doesn't start, or stops: that's the machines'
problem, not any clip's, so no clip is charged a failure. An item's failure
is charged to the clip only when no machine was at fault (the machine
answered, but not usefully) and a check started after the failure finds a
machine offering the model.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.client.cli.ai_pool import MachinePool

if TYPE_CHECKING:
    from src.client.cli.failure_report import FailureReport

logger = logging.getLogger(__name__)


class JobGuard:
    def __init__(self, client: Any, job: str, failures: FailureReport | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._failures = failures
        self._clock = clock
        self.pool = MachinePool(client, job, clock=clock)
        self.job = job
        self.label = self.pool.label
        self.error: str | None = None
        # When the last check started: a failure after it needs a new one.
        self._checked_at: float | None = None
        # Steps fail items from several threads at once: one check at a time.
        self._lock = threading.Lock()

    @property
    def model(self) -> str:
        return self.pool.model

    @property
    def down(self) -> bool:
        """No machine can be used: the job's work stops."""
        return self.error is not None

    def check(self) -> bool:
        """Read the account's model and machines and check each. True when the job's work can go ahead."""
        self._checked_at = self._clock()
        try:
            self.pool.check()
        except Exception as e:  # noqa: BLE001 — the server didn't answer: none of the job's work this time
            self.error = f"Couldn't read the {self.label.lower()} settings: {e}"
            logger.warning("%s: %s", self.job, self.error)
            return False
        self.error = self.pool.error
        if self.error:
            logger.warning("%s: %s %s waits until it's fixed.", self.job, self.error, self.label)
        return not self.down

    def describe(self) -> str:
        return self.pool.describe()

    def capacity(self) -> int:
        """Requests the online machines take at once, together (at least one)."""
        return max(1, self.pool.capacity())

    def on_fail(self, artifact: str) -> Callable[[str, object], bool]:
        """on_fail for one of the job's steps. A machine's fault reaches here
        only when no machine is left: the job's work stops, and no clip is
        charged. Otherwise the machines are checked again; the clip is charged
        only when a check started after its failure finds one offering the model.
        It returns whether the clip was charged (else it waits, uncharged)."""
        def fail(asset_id: str, error: object) -> bool:
            if getattr(error, "model_changed", False):  # caught in a model change: it waits
                return False
            if getattr(error, "endpoint_fault", False):
                with self._lock:
                    if not self.down:
                        self.error = str(error)
                        logger.warning("%s: %s %s waits until it's fixed.", self.job, self.error, self.label)
                return False
            failed_at = self._clock()
            with self._lock:
                if not self.down and (self._checked_at is None or self._checked_at <= failed_at):
                    self._checked_at = self._clock()
                    self.pool.recheck()
                    self.error = self.pool.error
            if self.down:
                return False
            # The machine that served it, found down since: its doing, not the clip's.
            served_by = getattr(error, "machine", None)
            if served_by is not None and not served_by.online:
                return False
            if self._failures is not None:
                self._failures.add(artifact, asset_id, error)
            return True
        return fail
