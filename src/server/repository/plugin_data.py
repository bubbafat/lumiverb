"""What plug-ins keep for the whole server (ADR-021), in the control
database: each one's state as it's fetched (plugin_data).

fetching (done of total, what it's doing), ready (how many, when), failed
(why). No row: never fetched.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

FETCHING, READY, FAILED = "fetching", "ready", "failed"


class PluginDataRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, plugin_id: str) -> dict[str, Any] | None:
        row = self._session.execute(text(
            "SELECT plugin_id, state, done, total, detail, error, count, started_at, ready_at, updated_at"
            " FROM plugin_data WHERE plugin_id = :p"), {"p": plugin_id}).mappings().first()
        return dict(row) if row else None

    def claim(self, plugin_id: str, *, now: datetime, stale_before: datetime, retry_before: datetime,
              retry_now: bool) -> bool:
        """Mark it being fetched, unless it's ready, being fetched (heard from
        since stale_before) or failed since retry_before (unless retry_now).
        True: the caller fetches it. Commits, so only one caller wins."""
        won = self._session.execute(text(
            "INSERT INTO plugin_data (plugin_id, state, done, total, detail, error, started_at, updated_at)"
            " VALUES (:p, :fetching, 0, NULL, NULL, NULL, :now, :now)"
            " ON CONFLICT (plugin_id) DO UPDATE SET state = :fetching, done = 0, total = NULL, detail = NULL,"
            "   error = NULL, started_at = :now, updated_at = :now"
            " WHERE (plugin_data.state = :fetching AND plugin_data.updated_at < :stale_before)"
            "    OR (plugin_data.state = :failed AND (:retry_now OR plugin_data.updated_at < :retry_before))"
            " RETURNING plugin_id"
        ), {"p": plugin_id, "fetching": FETCHING, "failed": FAILED, "now": now, "stale_before": stale_before,
            "retry_before": retry_before, "retry_now": retry_now}).first() is not None
        self._session.commit()
        return won

    def progress(self, plugin_id: str, done: int, total: int | None, detail: str, now: datetime) -> None:
        self._session.execute(text(
            "UPDATE plugin_data SET done = :done, total = :total, detail = :detail, updated_at = :now"
            " WHERE plugin_id = :p AND state = :fetching"
        ), {"p": plugin_id, "done": done, "total": total, "detail": detail, "now": now, "fetching": FETCHING})
        self._session.commit()

    def ready(self, plugin_id: str, count: int, now: datetime) -> None:
        self._session.execute(text(
            "UPDATE plugin_data SET state = :ready, count = :count, detail = NULL, error = NULL, ready_at = :now,"
            " updated_at = :now WHERE plugin_id = :p"
        ), {"p": plugin_id, "ready": READY, "count": count, "now": now})
        self._session.commit()

    def failed(self, plugin_id: str, error: str, now: datetime) -> None:
        self._session.execute(text(
            "UPDATE plugin_data SET state = :failed, error = :error, updated_at = :now WHERE plugin_id = :p"
        ), {"p": plugin_id, "failed": FAILED, "error": error[:500], "now": now})
        self._session.commit()
