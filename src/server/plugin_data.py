"""What a plug-in keeps for the whole server (ADR-021): fetched once, in the
background, when an account turns the plug-in on (its spec's `data`).

The fetch says how far it is in the control database (plugin_data), so
Settings › Plug-ins shows it. One that stops (the API restarted) is taken
up again by upkeep once it hasn't been heard from for STALE; one that failed
is tried again by upkeep after RETRY, or at once when an admin turns the
plug-in on again (Try again). Only one fetch of a plug-in runs at a time
(PluginDataRepository.claim).
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from datetime import timedelta
from typing import Any

from src.plugins import plugin as plugin_spec
from src.server.database import get_control_session
from src.server.repository.plugin_data import FETCHING, PluginDataRepository
from src.shared.utils import utcnow

logger = logging.getLogger(__name__)

STALE = timedelta(minutes=5)
RETRY = timedelta(hours=1)
# How often a fetch says how far it is, at most (seconds).
_EVERY = 1.0


def status(plugin_id: str) -> dict[str, Any] | None:
    """Its state for every account, or None for a plug-in that keeps nothing for the server.
    {"state": "missing"} before the first fetch."""
    spec = plugin_spec(plugin_id)
    if spec is None or not spec.data:
        return None
    with get_control_session() as session:
        return PluginDataRepository(session).get(plugin_id) or {"plugin_id": plugin_id, "state": "missing"}


def ensure(plugin_id: str, *, retry_now: bool = False, wait: bool = False) -> str:
    """Fetch what the plug-in keeps unless it's there or being fetched:
    "started", "running", "ready", "waiting" (failed less than RETRY ago,
    and not retry_now) or "none" (it keeps nothing). wait: in this thread,
    for tests and upkeep's callers that want the result."""
    spec = plugin_spec(plugin_id)
    if spec is None or not spec.data:
        return "none"
    now = utcnow()
    with get_control_session() as session:
        repo = PluginDataRepository(session)
        if not repo.claim(plugin_id, now=now, stale_before=now - STALE, retry_before=now - RETRY,
                          retry_now=retry_now):
            row = repo.get(plugin_id)
            state = row["state"] if row else "missing"
            return {"ready": "ready", FETCHING: "running"}.get(state, "waiting")
    if wait:
        _fetch(plugin_id, spec.data)
    else:
        threading.Thread(target=_fetch, args=(plugin_id, spec.data), name=f"plugin-data-{plugin_id}",
                         daemon=True).start()
    return "started"


def _load(target: str):
    module, _, name = target.partition(":")
    return getattr(importlib.import_module(module), name)


def _fetch(plugin_id: str, target: str) -> None:
    last = [0.0]

    def progress(done: int, total: int | None, detail: str, *, force: bool = False) -> None:
        if not force and time.monotonic() - last[0] < _EVERY:
            return
        last[0] = time.monotonic()
        with get_control_session() as session:
            PluginDataRepository(session).progress(plugin_id, done, total, detail, utcnow())

    try:
        count = _load(target)(progress)
    except Exception as exc:  # noqa: BLE001 — whatever went wrong is said in Settings › Plug-ins
        logger.warning("Fetching %s's data failed: %s", plugin_id, exc)
        with get_control_session() as session:
            PluginDataRepository(session).failed(plugin_id, str(exc) or type(exc).__name__, utcnow())
        return
    with get_control_session() as session:
        PluginDataRepository(session).ready(plugin_id, int(count), utcnow())
    logger.info("Fetched %s's data: %d", plugin_id, count)
