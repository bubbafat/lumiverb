"""Tell the server which items a step couldn't make (ADR-016 phase 3).

The server remembers each failure with its error and doesn't hand the item
out again for 5 minutes, then 10, 20 and so on up to a day, so one that
keeps failing doesn't sit first in every run, and a restart doesn't forget.
Failures are sent in batches; a server that predates this is told nothing.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

BATCH = 500


class FailureReport:
    def __init__(self, client: Any | None) -> None:
        self._client = client
        self._items: list[dict] = []
        self._lock = threading.Lock()
        self._off = client is None

    def add(self, artifact: str, asset_id: str, error: object) -> None:
        text = str(error).strip() or type(error).__name__
        with self._lock:
            self._items.append({"asset_id": asset_id, "artifact": artifact, "error": text[:2000]})
            full = len(self._items) >= BATCH
        if full:
            self.flush()

    def for_artifact(self, artifact: str):
        """add() for one artifact: (asset_id, error)."""
        return lambda asset_id, error: self.add(artifact, asset_id, error)

    def flush(self) -> None:
        with self._lock:
            items, self._items = self._items, []
        if not items or self._off:
            return
        for i in range(0, len(items), BATCH):
            try:
                self._client.post("/v1/producers/failures", json={"items": items[i:i + BATCH]})
            except Exception as e:  # noqa: BLE001 — never let reporting stop the work
                logger.warning("couldn't report %d failures to the server: %s", len(items) - i, e)
                if getattr(e, "status_code", None) in (404, 405):
                    self._off = True  # an older server
                return
