"""Vision work only while a machine doing it offers the account's model (ADR-016 phase 3).

Before descriptions, OCR or scene descriptions, the worker checks each of
the account's machines doing vision (Settings → AI) and tells the server
what each said; Settings shows it. Requests go to the online machines
(src/client/cli/ai_pool.py); one that fails is skipped and its item goes
to another. When no machine can be used, vision work doesn't start, or
stops: that's the machines' problem, not any clip's, so no clip is charged
a failure. An item's failure is charged to the clip only when no machine
was at fault (the model answered, but not usefully) and a check started
after the failure finds a machine offering the model.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.client.cli.ai_pool import MachinePool, PooledCaptionProvider

if TYPE_CHECKING:
    from src.client.cli.failure_report import FailureReport
    from src.client.workers.captions.base import CaptionProvider

logger = logging.getLogger(__name__)


class VisionGuard:
    def __init__(self, client: Any, failures: FailureReport | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._failures = failures
        self._clock = clock
        self.pool = MachinePool(client, "vision", clock=clock)
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
        """No machine can be used: vision work stops."""
        return self.error is not None

    def check(self) -> bool:
        """Read the account's model and machines and check each. True when vision work can go ahead."""
        self._checked_at = self._clock()
        try:
            self.pool.check()
        except Exception as e:  # noqa: BLE001 — the server didn't answer: no vision work this time
            self.error = f"Couldn't read the vision settings: {e}"
            logger.warning("vision: %s", self.error)
            return False
        self.error = self.pool.error
        if self.error:
            logger.warning("vision: %s Vision work waits until it's fixed.", self.error)
        return not self.down

    def describe(self) -> str:
        return self.pool.describe()

    def capacity(self) -> int:
        """Requests the online machines take at once, together (at least one)."""
        return max(1, self.pool.capacity())

    def provider(self, settings: dict | None = None, ocr_settings: dict | None = None) -> CaptionProvider:
        """Describes and reads images on whichever online machine is free."""
        from src.client.workers.captions.factory import get_caption_provider

        return PooledCaptionProvider(self.pool, lambda m: get_caption_provider(
            self.pool.model, m.api_url, m.api_key, settings=settings, ocr_settings=ocr_settings))

    def on_fail(self, artifact: str) -> Callable[[str, object], None]:
        """on_fail for a vision step. A machine's fault reaches here only when
        no machine is left: vision work stops, and no clip is charged.
        Otherwise the machines are checked again; the clip is charged only
        when a check started after its failure finds one offering the model."""
        def fail(asset_id: str, error: object) -> None:
            if getattr(error, "endpoint_fault", False):
                with self._lock:
                    if not self.down:
                        self.error = str(error)
                        logger.warning("vision: %s Vision work waits until it's fixed.", self.error)
                return
            failed_at = self._clock()
            with self._lock:
                if not self.down and (self._checked_at is None or self._checked_at <= failed_at):
                    self._checked_at = self._clock()
                    self.pool.recheck()
                    self.error = self.pool.error
            if self.down:
                return
            # The machine that served it, found down since: its doing, not the clip's.
            served_by = getattr(error, "machine", None)
            if served_by is not None and not served_by.online:
                return
            if self._failures is not None:
                self._failures.add(artifact, asset_id, error)
        return fail
