"""Artifact files that went missing: reported by the GET that found one,
repaired by upkeep.

A GET never changes a clip. When `GET /v1/assets/{id}/artifacts/{type}`
finds the clip's key set but its file gone, it answers 404 artifact_missing
and reports the clip here: a list in the account's system_metadata, written
in its own transaction, at most once per clip and kind per API worker every
REPORT_EVERY_SEC, and never past MAX_REPORTED entries. Best effort:
reporting never fails the request.

Upkeep (`POST /v1/upkeep`, every 5 minutes) takes the reports, checks each
file again, and clears the keys of those still gone, so the producers make
them again (a producer's "made" is its key being set). A thumbnail is the
proxy producer's, so a missing thumbnail clears the proxy's key too.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from sqlalchemy import text
from sqlmodel import Session

from src.server.storage.local import get_storage

logger = logging.getLogger(__name__)

KEY = "artifacts.missing"
MAX_REPORTED = 1000
REPORT_EVERY_SEC = 600.0

# The asset columns each reportable kind's file is named by; the first is the key.
COLUMNS: dict[str, tuple[str, ...]] = {
    "proxy": ("proxy_key",),
    "thumbnail": ("thumbnail_key", "proxy_key"),
    "video_preview": ("video_preview_key",),
    "analysis_proxy": ("analysis_proxy_key", "analysis_proxy_sha256"),
}

_reported: dict[tuple[str, str, str], float] = {}
_lock = threading.Lock()


def report(session: Session, asset_id: str, kind: str) -> None:
    """Report that the clip's `kind` file is gone, for upkeep to repair."""
    if kind not in COLUMNS:
        return
    bind = session.get_bind()
    slot = (str(getattr(bind, "url", bind)), asset_id, kind)
    now = time.monotonic()
    with _lock:
        if now - _reported.get(slot, float("-inf")) < REPORT_EVERY_SEC:
            return
        if len(_reported) > 10 * MAX_REPORTED:
            _reported.clear()
        _reported[slot] = now
    entry = f"{asset_id}:{kind}"
    try:
        with Session(bind) as own:
            own.execute(text(
                "INSERT INTO system_metadata (key, value, updated_at) VALUES (:k, :v, now())"
                " ON CONFLICT (key) DO UPDATE SET updated_at = now(), value = CASE"
                "   WHEN system_metadata.value::jsonb ? :e"
                "     OR jsonb_array_length(system_metadata.value::jsonb) >= :max"
                "   THEN system_metadata.value"
                "   ELSE (system_metadata.value::jsonb || EXCLUDED.value::jsonb)::text END"),
                {"k": KEY, "v": json.dumps([entry]), "e": entry, "max": MAX_REPORTED})
            own.commit()
    except Exception:  # noqa: BLE001 — only a report
        logger.warning("Couldn't report missing %s of %s", kind, asset_id, exc_info=True)


def reported(session: Session) -> list[str]:
    """The reports waiting for upkeep, as "asset_id:kind"."""
    raw = session.execute(text("SELECT value FROM system_metadata WHERE key = :k"), {"k": KEY}).scalar()
    try:
        return [e for e in json.loads(raw) if isinstance(e, str)] if raw else []
    except ValueError:
        return []


def repair(session: Session) -> dict[str, int]:
    """Take the reports and clear the keys whose files are still gone.
    Returns {"checked", "cleared"}."""
    raw = session.execute(text("DELETE FROM system_metadata WHERE key = :k RETURNING value"), {"k": KEY}).scalar()
    try:
        entries = [e for e in json.loads(raw) if isinstance(e, str)] if raw else []
    except ValueError:
        entries = []
    storage = get_storage()
    checked = cleared = 0
    for entry in entries:
        asset_id, _, kind = entry.rpartition(":")
        columns = COLUMNS.get(kind)
        if not asset_id or columns is None:
            continue
        checked += 1
        key = session.execute(text(f"SELECT {columns[0]} FROM assets WHERE asset_id = :a"),  # noqa: S608 — fixed names
                              {"a": asset_id}).scalar()
        if not key:
            continue
        try:
            gone = not storage.abs_path(key).is_file()
        except ValueError:  # a key storage refuses is as good as gone
            gone = True
        if gone:
            sets = ", ".join(f"{c} = NULL" for c in columns)
            session.execute(text(f"UPDATE assets SET {sets} WHERE asset_id = :a"), {"a": asset_id})  # noqa: S608
            cleared += 1
            logger.info("Cleared the missing %s of %s for its producer to make again", kind, asset_id)
    session.commit()
    return {"checked": checked, "cleared": cleared}
