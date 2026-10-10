"""Tell the server which items a step couldn't make (ADR-016 phase 3).

The server remembers each failure with its error and doesn't hand the item
out again for 5 minutes, then 10, 20 and so on up to a day, so one that
keeps failing doesn't sit first in every run, and a restart doesn't forget.
Failures are sent in batches. A batch that can't be sent (the server away)
is kept for the next flush. Any other error splits it until the item that
causes it is alone: one the server refuses is dropped; one that fails
otherwise is kept, and dropped after SINGLE_TRIES flushes.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger(__name__)

BATCH = 500
# Failures kept while the server can't be reached, at most (the oldest go).
KEEP = 20 * BATCH
# After a flush that couldn't send, a full batch waits this long before
# trying again on its own (the scheduler flushes every few seconds anyway).
RETRY_SEC = 10.0
# The server away or busy, or who asked: the batch is kept as it is.
_AWAY = {401, 403, 408, 429, 502, 503, 504}
# What the server refuses for what's in it: the item is dropped.
_REFUSED = {400, 413, 422}
# Flushes one item alone may fail otherwise (a 500, say) before it's dropped.
SINGLE_TRIES = 3
# The longest asset id the server takes (routers/producers.py Failure).
MAX_ID = 64


# How long a failure is remembered here, for charged(): longer than any job runs.
REMEMBER_SEC = 6 * 3600.0


class FailureReport:
    def __init__(self, client: Any | None, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._client = client
        self._items: list[dict] = []
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()  # one flush at a time: what's kept stays in order
        self._off = client is None
        self._clock = clock
        self._charged: dict[tuple[str, str], float] = {}  # (artifact, asset_id) → when
        self._pruned_at = float("-inf")
        self._unsent_at = float("-inf")
        self._tries: dict[int, int] = {}  # id(item) → failed sends alone

    def add(self, artifact: str, asset_id: str, error: object) -> None:
        from src.shared.producers import PRODUCERS

        if artifact not in PRODUCERS or not isinstance(asset_id, str) or not 0 < len(asset_id) <= MAX_ID:
            logger.warning("not reporting a failure the server can't take: %r for %r", artifact, asset_id)
            return
        # Postgres takes no NUL in text, and JSON no lone surrogate (a path
        # that isn't UTF-8, in ffprobe's words).
        text = (str(error).replace("\x00", "").encode("utf-8", "replace").decode().strip()
                or type(error).__name__)
        now = self._clock()
        with self._lock:
            self._items.append({"asset_id": asset_id, "artifact": artifact, "error": text[:2000]})
            self._charged[(artifact, asset_id)] = now
            if len(self._charged) > 10_000 and now - self._pruned_at > 60:  # at most once a minute
                self._pruned_at = now
                self._charged = {k: t for k, t in self._charged.items() if now - t < REMEMBER_SEC}
            full = len(self._items) >= BATCH and now - self._unsent_at >= RETRY_SEC
        if full:
            self.flush()

    def mark(self) -> float:
        """Now, on this report's clock: what charged() takes as since."""
        return self._clock()

    def charged(self, artifact: str, asset_ids: Iterable[str], *, since: float) -> set[str]:
        """Which of these clips a failure was charged to for this artifact
        since then (this report's clock): the scheduler learns its pace only
        from the clips a job made."""
        with self._lock:
            return {i for i in asset_ids if self._charged.get((artifact, i), float("-inf")) >= since}

    def for_artifact(self, artifact: str):
        """add() for one artifact: (asset_id, error)."""
        return lambda asset_id, error: self.add(artifact, asset_id, error)

    def pending(self) -> int:
        """Failures not sent yet."""
        with self._lock:
            return len(self._items)

    def flush(self) -> None:
        """Send what's waiting. What couldn't be sent stays for the next flush."""
        if self._off:
            return
        with self._send_lock:
            with self._lock:
                items, self._items = self._items, []
            for i in range(0, len(items), BATCH):
                left = self._send(items[i:i + BATCH])
                if left:
                    self._keep(left + items[i + BATCH:])
                    return

    def _send(self, batch: list[dict]) -> list[dict]:
        """Send one batch; returns what couldn't be sent now (the server away).
        Any other error splits it in two until the item that causes it is
        alone: refused, it's dropped; otherwise it's kept, up to SINGLE_TRIES."""
        import httpx

        try:
            self._client.post("/v1/producers/failures", json={"items": batch})
            for item in batch:
                self._tries.pop(id(item), None)
            return []
        except Exception as e:  # noqa: BLE001 — never let reporting stop the work
            status = getattr(e, "status_code", None)
            if status in _AWAY or isinstance(e, httpx.TransportError):
                logger.warning("couldn't report %d failures to the server (kept for later): %s", len(batch), e)
                return batch
            if len(batch) == 1:
                item = batch[0]
                tries = self._tries.pop(id(item), 0) + 1
                if status in _REFUSED or tries >= SINGLE_TRIES:
                    logger.warning("couldn't report the failure of %s (%s), dropped: %s",
                                   item.get("asset_id"), item.get("artifact"), e)
                    return []
                self._tries[id(item)] = tries
                return batch
        mid = len(batch) // 2
        left = self._send(batch[:mid])
        return left + batch[mid:] if left else self._send(batch[mid:])

    def _keep(self, unsent: list[dict]) -> None:
        """Put what couldn't be sent back first, keeping at most KEEP (the oldest go)."""
        with self._lock:
            self._unsent_at = self._clock()
            kept = unsent + self._items
            if len(kept) > KEEP:
                logger.warning("dropping %d failure reports the server hasn't taken (keeping %d)",
                               len(kept) - KEEP, KEEP)
                kept = kept[-KEEP:]
            self._items = kept
            alive = {id(item) for item in kept}
            self._tries = {k: n for k, n in self._tries.items() if k in alive}
