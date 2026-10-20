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
them again (a producer's "made" is its key being set). A thumbnail is made
again from the clip's preview, which stays as it is (the server made it
from that preview in the first place). Only when the preview is gone too
are both keys cleared: a thumbnail is the proxy producer's, and a scan
makes the two again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time

from sqlalchemy import text
from sqlmodel import Session

from src.producers.names import ANALYSIS_PROXY, PREVIEW, THUMBNAIL, VIDEO_PREVIEW, file_in
from src.server.storage.store import NotFound, Store, Unavailable, get_store

logger = logging.getLogger(__name__)

KEY = "artifacts.missing"
MAX_REPORTED = 1000
REPORT_EVERY_SEC = 600.0

# The asset columns each reportable kind's file is named by; the first is the key.
COLUMNS: dict[str, tuple[str, ...]] = {
    PREVIEW: ("proxy_key",),
    THUMBNAIL: ("thumbnail_key", "proxy_key"),
    VIDEO_PREVIEW: ("video_preview_key",),
    ANALYSIS_PROXY: ("analysis_proxy_key", "analysis_proxy_sha256"),
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
    """The reports waiting for upkeep, as "asset_id:file" (the file by its name)."""
    raw = session.execute(text("SELECT value FROM system_metadata WHERE key = :k"), {"k": KEY}).scalar()
    try:
        return [e for e in json.loads(raw) if isinstance(e, str)] if raw else []
    except ValueError:
        return []


def repair(session: Session) -> dict[str, int]:
    """Take the reports, make a thumbnail still gone again from its clip's
    preview, and clear the keys of other files still gone. Returns
    {"checked", "cleared", "remade"}."""
    nothing = {"checked": 0, "cleared": 0, "remade": 0}
    raw = session.execute(text("DELETE FROM system_metadata WHERE key = :k RETURNING value"), {"k": KEY}).scalar()
    try:
        entries = [e for e in json.loads(raw) if isinstance(e, str)] if raw else []
    except ValueError:
        entries = []
    store = get_store()
    checked = cleared = remade = 0
    for entry in entries:
        asset_id, _, kind = entry.rpartition(":")
        kind = file_in(kind)  # by its name, or the kind a report had before ADR-024's part 4 (until part 5)
        columns = COLUMNS.get(kind)
        if not asset_id or columns is None:
            continue
        checked += 1
        key = session.execute(text(f"SELECT {columns[0]} FROM assets WHERE asset_id = :a"),  # noqa: S608 — fixed names
                              {"a": asset_id}).scalar()
        if not key:
            continue
        try:
            if _storage_gone(store, key):
                logger.warning("Missing files not repaired: its library has no files kept at all (a disk unmounted?)")
                session.rollback()  # the reports stay for the next upkeep
                return nothing
            gone = not store.exists(key)
        except Unavailable as exc:
            logger.warning("Missing files not repaired: %s", exc)
            session.rollback()  # the reports stay for the next upkeep
            return nothing
        except ValueError:  # a key the store refuses is as good as gone
            gone = True
        if gone and kind == THUMBNAIL and _thumbnail_again(session, store, asset_id, key):
            remade += 1
            logger.info("Made the missing thumbnail of %s again from its preview", asset_id)
        elif gone:
            sets = ", ".join(f"{c} = NULL" for c in columns)
            session.execute(text(f"UPDATE assets SET {sets} WHERE asset_id = :a"), {"a": asset_id})  # noqa: S608
            cleared += 1
            logger.info("Cleared the missing %s of %s for its producer to make again", kind, asset_id)
    session.commit()
    return {"checked": checked, "cleared": cleared, "remade": remade}


def _thumbnail_again(session: Session, store: Store, asset_id: str, key: str) -> bool:
    """Make a clip's thumbnail again at its key from its preview, as ingest
    made it. False when the preview is gone too, or isn't an image."""
    from src.server.preview_image import NotAnImageError, quality, thumbnail

    proxy_key = session.execute(text("SELECT proxy_key FROM assets WHERE asset_id = :a"), {"a": asset_id}).scalar()
    if not proxy_key:
        return False
    try:
        data = thumbnail(store.read(proxy_key), quality(session))
        store.write(key, data)
    except NotFound:
        return False
    except (ValueError, OSError, NotAnImageError) as exc:  # NotAnImageError is a ValueError
        logger.warning("Couldn't make the thumbnail of %s again from its preview: %s", asset_id, exc)
        return False
    session.execute(text("UPDATE assets SET thumbnail_sha256 = :s WHERE asset_id = :a"),
                    {"s": hashlib.sha256(data).hexdigest(), "a": asset_id})
    return True


def _storage_gone(store: Store, key: str) -> bool:
    """No file kept for the key's library (its account, for a key of no
    library) at all: an unmounted disk (its empty mount point), not files
    gone one by one. Unavailable (where it's kept can't be reached) propagates."""
    parts = key.split("/", 2)
    where = "/".join(parts[:2] if len(parts) > 2 else parts[:1])
    return next(iter(store.list(f"{where}/")), None) is None
