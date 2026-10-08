"""Vision work only while the account's endpoint offers its model (ADR-016 phase 3).

Before descriptions, OCR or scene descriptions, the worker asks the
endpoint chosen in Settings → AI whether it still offers the model, and
tells the server what it found; Settings shows a problem until it's fixed.
When the endpoint can't be used, vision work doesn't start, or stops: that
is the endpoint's problem, not any clip's, so no clip is charged a failure.
An item's failure is charged to the clip only when the endpoint wasn't at
fault (the model answered, but not usefully) and a check started after the
failure finds the endpoint offering the model.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.shared.vision_endpoint import check_model

if TYPE_CHECKING:
    from src.client.cli.failure_report import FailureReport

logger = logging.getLogger(__name__)

class VisionGuard:
    def __init__(self, client: Any, failures: FailureReport | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._client = client
        self._failures = failures
        self._clock = clock
        self.api_url = ""
        self.api_key: str | None = None
        self.model = ""
        self.error: str | None = None
        # When the last check started: a failure after it needs a new one.
        self._checked_at: float | None = None
        # Steps fail items from several threads at once: one check at a time.
        self._lock = threading.Lock()

    @property
    def down(self) -> bool:
        """The endpoint can't be used: vision work stops."""
        return self.error is not None

    def check(self) -> bool:
        """Read the account's endpoint and model, ask the endpoint, and tell
        the server. True when vision work can go ahead."""
        from src.client.cli.ingest import _resolve_vision_config

        try:
            self.api_url, self.api_key, self.model, _ = _resolve_vision_config(self._client)
        except Exception as e:  # noqa: BLE001 — the server didn't answer: no vision work this time
            self.error = f"Couldn't read the vision settings: {e}"
            logger.warning("vision: %s", self.error)
            return False
        self._ask()
        return not self.down

    def _ask(self) -> None:
        self._checked_at = self._clock()
        if not self.api_url or not self.model:
            self.error = "No vision model is chosen. An admin picks one in Settings → AI."
        else:
            self.error = check_model(self.api_url, self.api_key, self.model)
        if self.error:
            logger.warning("vision: %s Vision work waits until it's fixed.", self.error)
        self._report()

    def _report(self) -> None:
        if not self.api_url and not self.model:
            return  # vision AI is off: nothing to show
        try:
            self._client.post("/v1/tenant/vision/status", json={
                "ok": not self.down, "error": self.error or "", "model": self.model, "api_url": self.api_url})
        except Exception as e:  # noqa: BLE001 — never let reporting stop the work
            logger.warning("vision: couldn't tell the server what the endpoint said: %s", e)

    def on_fail(self, artifact: str) -> Callable[[str, object], None]:
        """on_fail for a vision step. The endpoint's fault (it didn't answer,
        refused, is overloaded, lost the model) stops vision work and charges
        no clip. Otherwise the endpoint is asked again; the clip is charged
        only when a check started after its failure finds the model offered."""
        def fail(asset_id: str, error: object) -> None:
            if getattr(error, "endpoint_fault", False):
                with self._lock:
                    if not self.down:
                        self.error = f"The vision endpoint failed: {error}"
                        self._checked_at = self._clock()
                        logger.warning("vision: %s Vision work waits until it's fixed.", self.error)
                        self._report()
                return
            failed_at = self._clock()
            with self._lock:
                if not self.down and (self._checked_at is None or self._checked_at <= failed_at):
                    self._ask()
            if self.down:
                return
            if self._failures is not None:
                self._failures.add(artifact, asset_id, error)
        return fail
