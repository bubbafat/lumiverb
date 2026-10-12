"""Which Quickwit index holds each kind of an account's search documents.

Each kind (a clip's own text, its scenes, its transcript's segments) has a
live index, which searches read. One made again (its schema changed, the
database was restored, an admin asked) is built beside it: every write goes
to both while the search sweep fills the new one from the database (its
cursor: how far, by id), then the new one is live, and the old one, still
written to for one more sweep, goes. Search keeps answering from the old one
meanwhile (ADR-016, principle 11). Recorded in the account's system_metadata
(search.indexes); an account that never rebuilt one has the original names.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass

from sqlalchemy import text
from sqlmodel import Session

logger = logging.getLogger(__name__)

KINDS = ("assets", "scenes", "transcripts")
KEY = "search.indexes"
_SUFFIX = {"assets": "", "scenes": "_scenes", "transcripts": "_transcripts"}
# How long a process trusts what it read: a write starts going to a new
# index this long after the rebuild begins at the latest, and the sweep
# begins filling it only on its next pass, minutes later.
TTL_SEC = 2.0


def base_id(tenant_id: str, kind: str) -> str:
    """The index a kind's documents have always been in."""
    return f"lumiverb_tenant_{tenant_id}{_SUFFIX[kind]}"


@dataclass
class Where:
    live: str
    building: str | None = None
    retired: str | None = None
    # How far the building index is filled: the last id put in ("" once
    # begun, None until the sweep after the rebuild began).
    cursor: str | None = None

    def targets(self) -> list[str]:
        """Every index a write goes to: the live one, and one built or retired beside it."""
        return [i for i in (self.live, self.building, self.retired) if i]


def read(session: Session, tenant_id: str) -> dict[str, Where]:
    """Each kind's indexes, as the account's database says now."""
    raw = session.execute(text("SELECT value FROM system_metadata WHERE key = :k"), {"k": KEY}).scalar()
    try:
        saved = json.loads(raw) if raw else {}
    except ValueError:
        saved = {}
    return {kind: Where(**saved[kind]) if isinstance(saved.get(kind), dict) else Where(live=base_id(tenant_id, kind))
            for kind in KINDS}


def write(session: Session, tenant_id: str, where: dict[str, Where]) -> None:
    """Record each kind's indexes, in the caller's transaction (it commits)."""
    session.execute(text(
        "INSERT INTO system_metadata (key, value, updated_at) VALUES (:k, :v, now())"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
        {"k": KEY, "v": json.dumps({kind: asdict(w) for kind, w in where.items()})})
    with _lock:
        _cache.pop(tenant_id, None)


def next_id(tenant_id: str, kind: str, where: Where) -> str:
    """A name for a new index of the kind, unlike the ones it has."""
    base = base_id(tenant_id, kind)
    taken = {i for i in where.targets()}
    n = 1
    while f"{base}_g{n}" in taken:
        n += 1
    return f"{base}_g{n}"


_cache: dict[str, tuple[float, dict[str, Where]]] = {}
_lock = threading.Lock()
_said_unreadable: set[str] = set()


def cached(tenant_id: str) -> dict[str, Where]:
    """Each kind's indexes for a Quickwit call, read at most every TTL_SEC.
    An account whose database can't be read has the original names."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(tenant_id)
        if hit is not None and now - hit[0] < TTL_SEC:
            return hit[1]
    try:
        from src.server.database import get_tenant_session

        with get_tenant_session(tenant_id) as session:
            where = read(session, tenant_id)
    except Exception as exc:  # noqa: BLE001 — no database to ask: the names indexes have always had
        if tenant_id not in _said_unreadable:
            _said_unreadable.add(tenant_id)
            logger.warning("Search indexes of %s not readable (%s): using the original names", tenant_id, exc)
        return {kind: Where(live=base_id(tenant_id, kind)) for kind in KINDS}
    with _lock:
        _cache[tenant_id] = (now, where)
    return where
