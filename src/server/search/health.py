"""What search went through lately, for the Admin page's Search row.

A search that fell back to Postgres because Quickwit failed, or that failed
outright, is noted in the account's system_metadata (so every API worker
sees it): when and why, the latest of each. Each worker writes a note at
most once a minute per account and kind, so a burst of searches during an
outage doesn't turn into a burst of writes. Best effort: noting never
fails a search.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime

from sqlalchemy import text
from sqlmodel import Session

from src.shared.utils import utcnow

logger = logging.getLogger(__name__)

FALLBACK = "search.fallback"
FAILURE = "search.failure"
NOTE_EVERY_SEC = 60.0

_noted: dict[tuple[str, str], float] = {}
_lock = threading.Lock()


def note(session: Session, key: str, reason: str) -> None:
    """Note a fallback (FALLBACK) or failure (FAILURE) with why, in its own
    transaction on the session's database (the request's isn't touched)."""
    bind = session.get_bind()
    slot = (str(getattr(bind, "url", bind)), key)
    now = time.monotonic()
    with _lock:
        if now - _noted.get(slot, float("-inf")) < NOTE_EVERY_SEC:
            return
        _noted[slot] = now
    try:
        with Session(bind) as own:
            own.execute(text(
                "INSERT INTO system_metadata (key, value, updated_at) VALUES (:k, :v, now())"
                " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
                {"k": key, "v": json.dumps({"at": utcnow().isoformat(), "reason": reason[:300]})})
            own.commit()
    except Exception:  # noqa: BLE001 — only a note
        logger.warning("Couldn't note %s", key, exc_info=True)


def last(session: Session, key: str) -> tuple[datetime, str] | None:
    """(when, why) of the latest note of the kind; None when there's none."""
    raw = session.execute(text("SELECT value FROM system_metadata WHERE key = :k"), {"k": key}).scalar()
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return datetime.fromisoformat(value["at"]), str(value.get("reason") or "")
    except (ValueError, KeyError, TypeError):
        return None
