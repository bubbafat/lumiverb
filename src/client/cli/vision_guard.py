"""Vision work only while the account's endpoint offers its model (ADR-016 phase 3).

Before descriptions, OCR or scene descriptions, the worker asks the
endpoint chosen in Settings → AI whether it still offers the model, and
tells the server what it found; Settings shows a problem until it's fixed.
When the endpoint can't be used, vision work doesn't start, or stops: that
is the endpoint's problem, not any clip's, so no clip is charged a failure.
An item that fails while the endpoint is fine is the clip's.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.shared.vision_endpoint import check_model

if TYPE_CHECKING:
    from src.client.cli.failure_report import FailureReport

logger = logging.getLogger(__name__)

# An item's failure re-asks the endpoint at most this often.
RECHECK_SEC = 30.0


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
        self._checked_at: float | None = None

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
        if not self.api_url or not self.model:
            self.error = "No vision model is chosen. An admin picks one in Settings → AI."
        else:
            self.error = check_model(self.api_url, self.api_key, self.model)
        self._checked_at = self._clock()
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
        """on_fail for a vision step: the clip's failure, unless the endpoint
        no longer offers the model; then work stops and no clip is charged."""
        def fail(asset_id: str, error: object) -> None:
            if not self.down and (self._checked_at is None or self._clock() - self._checked_at >= RECHECK_SEC):
                self._ask()
            if self.down:
                return
            if self._failures is not None:
                self._failures.add(artifact, asset_id, error)
        return fail
