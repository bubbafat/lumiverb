"""What's due, straight from an account's database (ADR-016 phase 4).

The reconciler (repository/lineage.py) says what each clip still needs:
missing, or made from a file whose content has since changed, less failures
waiting their turn; and for a redo kind, made with another producer,
version or settings than now (lineage.queued, the rule a library's reader is
given too, src/server/reader_work.py). Here each kind's due clips are listed oldest
first (in the order they were added), across the libraries asked about,
with what the job needs to know about each: the keys of its stored preview
and analysis proxy among them, which its work reads by (inputs.py).

Work on the originals is listed only in the libraries this server reads
(and can reach now); work from what's stored (previews, analysis proxies) in
every active library, a device's too (ADR-018 decision 2): the scheduler
asks for those with no library list.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.server.scheduler.kinds import Kind

BUFFER = 200  # due clips listed per kind at a time


def candidates(session: Session, kind: Kind, library_ids: list[str] | None, limit: int = BUFFER,
               skip: list[str] | None = None, want: dict[str, Any] | None = None) -> list[dict]:
    """The kind's due clips in these libraries (None: every active library,
    whoever reads it), oldest first, at most limit, less skip (clips in
    hand or just tried: they mustn't fill the answer). A redo kind needs
    want: what its producer makes now (lineage.desired)."""
    from src.server.repository import lineage
    from src.server.repository.lineage import lineage_join

    if not kind.flag or library_ids == []:
        return []
    if kind.storage and library_ids is None:
        raise ValueError(f"{kind.name} reads the originals: only the libraries this server reads")
    params: dict[str, Any] = {"libs": library_ids, "n": limit, "skip": list(skip or [])}
    if kind.redo:
        if want is None:
            raise ValueError(f"{kind.name}: what its producer makes now is needed")
        params |= lineage.redo_params(kind.artifact, want)
    # The one rule: for a producer that reads the originals, the list any reader gets (src/server/reader_work.py).
    condition = lineage.queued(kind.artifact, redo=kind.redo)
    rows = session.execute(text(
        "SELECT a.asset_id, a.library_id, a.rel_path, a.media_type, a.content_hash, a.duration_sec,"
        " a.created_at, a.proxy_key, a.analysis_proxy_key, a.analysis_proxy_key IS NOT NULL AS has_analysis_proxy"
        f" FROM active_assets a {lineage_join()}"
        " WHERE (CASE WHEN CAST(:libs AS text[]) IS NULL"
        "             THEN a.library_id IN (SELECT library_id FROM libraries WHERE status = 'active')"
        "             ELSE a.library_id = ANY(CAST(:libs AS text[])) END)"
        f"   AND {condition}"
        "   AND a.asset_id <> ALL(CAST(:skip AS text[]))"
        " ORDER BY a.created_at, a.asset_id LIMIT :n"
    ), params).mappings().all()
    # A redo says what it's to be made with: the scheduler reads the settings
    # again when they're newer than those it has.
    made_with = {"settings_hash": want["settings_hash"]} if kind.redo else {}
    return [{**r, "created_at": r["created_at"].isoformat() if r["created_at"] else "", "redo": kind.redo,
             **made_with} for r in rows]
