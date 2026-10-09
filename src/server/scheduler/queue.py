"""What's due, straight from an account's database (ADR-016 phase 4).

The reconciler (repository/lineage.py) says what each clip still needs:
missing, or made from a file whose content has since changed, less failures
waiting their turn. Here each kind's due clips are listed oldest first (in
the order they were added), across the libraries asked about, with what
the job needs to know about each.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session

from src.server.scheduler.kinds import Kind

BUFFER = 200  # due clips listed per kind at a time


def candidates(session: Session, kind: Kind, library_ids: list[str], limit: int = BUFFER,
               skip: list[str] | None = None) -> list[dict]:
    """The kind's due clips in these libraries, oldest first, at most limit,
    less skip (clips in hand or just tried: they mustn't fill the answer)."""
    from src.server.repository.lineage import LINEAGE_JOIN
    from src.server.repository.tenant import MISSING_CONDITIONS

    if not kind.flag or not library_ids:
        return []
    rows = session.execute(text(
        "SELECT a.asset_id, a.library_id, a.rel_path, a.media_type, a.sha256, a.duration_sec,"
        " a.created_at, a.analysis_proxy_key IS NOT NULL AS has_analysis_proxy"
        f" FROM active_assets a {LINEAGE_JOIN}"
        f" WHERE a.library_id = ANY(:libs) AND ({MISSING_CONDITIONS[kind.flag]}) AND ({kind.extra})"
        "   AND a.asset_id <> ALL(CAST(:skip AS text[]))"
        " ORDER BY a.created_at, a.asset_id LIMIT :n"
    ), {"libs": library_ids, "n": limit, "skip": list(skip or [])}).mappings().all()
    return [{**r, "created_at": r["created_at"].isoformat() if r["created_at"] else ""} for r in rows]
