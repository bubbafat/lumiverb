"""System health for the Admin page (GET /v1/system/health).

Six rows (website, processing, AI machines, search, storage, disk), each
green, yellow or red with a one-line reason and where to fix it; the rules
are src/server/system_health.py. Anyone signed in sees it; a public page's
visitor doesn't. The web page asks every 10 seconds, so what costs more
than a query (asking Quickwit, counting clips) is kept for a little while
per worker.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import Session

from src.server import system_health as h
from src.server.api.dependencies import get_tenant_session, require_signed_in
from src.shared.producers import PAUSE_SCANS
from src.shared.utils import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/system", tags=["system"])

# How long each worker keeps what's slow to find out, per account.
QUICKWIT_EVERY_SEC = 30.0
COUNTS_EVERY_SEC = 60.0

_cache: dict[tuple[str, str], tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _kept[T](tenant_id: str, name: str, every: float, find: Callable[[], T]) -> T:
    """find(), or what it gave less than `every` seconds ago for the account."""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get((tenant_id, name))
    if hit and now - hit[0] < every:
        return hit[1]
    value = find()
    with _cache_lock:
        _cache[(tenant_id, name)] = (now, value)
    return value


class HealthRow(BaseModel):
    key: str  # website | processing | ai | search | storage | disk
    title: str
    state: str  # green | yellow | red
    reason: str
    link: str | None = None  # the web app's page where it's fixed
    checked_at: datetime | None = None


class LibraryReach(BaseModel):
    library_id: str
    name: str
    # Whether the scheduler's latest look since it started reached its storage;
    # None when that isn't known now (not looked at since, Scans paused, or the
    # scheduler isn't running).
    reachable: bool | None
    seen_at: datetime | None = None  # last seen reachable


class SystemHealth(BaseModel):
    state: str  # the worst row's
    rows: list[HealthRow]
    libraries: list[LibraryReach]


@router.get("/health", response_model=SystemHealth, dependencies=[Depends(require_signed_in)])
def system_health(request: Request, session: Annotated[Session, Depends(get_tenant_session)]) -> SystemHealth:
    """Each part of the system green, yellow or red, why, and where to fix it."""
    tenant_id = request.state.tenant_id
    now = utcnow()

    db_error = None
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — that's the answer
        session.rollback()
        db_error = _short(exc)

    status, unreadable = None, False
    if db_error is None:
        try:
            from src.server.api.routers.producers import scheduler_status

            status = scheduler_status(request, session)
        except Exception:  # noqa: BLE001
            logger.exception("system health: reading the scheduler's status failed")
            session.rollback()
            unreadable = True

    rows = [
        _row("website", "Website", lambda: _website(session, db_error, now), session=session),
        _row("processing", "Processing", lambda: _processing(session, tenant_id, status, now, unreadable), db_error, session),
        _row("ai", "AI machines", lambda: _ai(tenant_id, status, now), db_error),
        _row("search", "Search", lambda: _search(session, tenant_id, now), db_error, session),
    ]
    libraries: list[tuple[str, str]] = []
    at = status.at if status else None
    seen = {i: r.model_dump() for i, r in status.storage.items()} if status else {}
    scans_paused = bool(status and any(sw.target == PAUSE_SCANS and sw.paused for sw in status.switches))

    def storage() -> h.Row:
        from src.server.repository.tenant import LibraryRepository

        libraries.extend((lib.library_id, lib.name) for lib in LibraryRepository(session).list_all())
        return h.storage_row(libraries=libraries, storage=seen, scans_paused=scans_paused, at=at, now=now)

    rows.append(_row("storage", "Storage", storage, db_error, session))
    rows.append(_row("disk", "Disk", lambda: _disk(now)))

    return SystemHealth(
        state=h.overall(rows),
        rows=[HealthRow(**r.as_dict()) for r in rows],
        libraries=[LibraryReach(library_id=i, name=n, seen_at=(seen.get(i) or {}).get("seen_at"),
                                reachable=h.library_reachable(seen.get(i), at=at, now=now, scans_paused=scans_paused))
                   for i, n in libraries],
    )


def _short(exc: BaseException) -> str:
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0][:200]


def _row(key: str, title: str, make: Callable[[], h.Row], db_error: str | None = None,
         session: Session | None = None) -> h.Row:
    """make(), or a yellow row saying it couldn't be checked (the database
    being down is the website row's red; the others just can't tell)."""
    if db_error is not None:
        return h.Row(key, title, h.YELLOW, "Can't be checked while the database isn't answering.")
    try:
        return make()
    except Exception as exc:  # noqa: BLE001 — one row never takes the page down
        logger.exception("system health: checking %s failed", key)
        if session is not None:  # a failed query aborts the transaction: the next rows' queries need it back
            session.rollback()
        return h.Row(key, title, h.YELLOW, f"Couldn't be checked: {_short(exc)}")


def _website(session: Session, db_error: str | None, now: datetime) -> h.Row:
    maintenance = None
    if db_error is None:
        from src.server.api.routers.maintenance import get_maintenance_state

        state = get_maintenance_state(session)
        if state.get("active"):
            maintenance = state.get("message") or ""
    return h.website_row(db_error=db_error, now=now, maintenance=maintenance)


def _processing(session: Session, tenant_id: str, status: Any, now: datetime, unreadable: bool = False) -> h.Row:
    def failing() -> int:
        return session.execute(text(
            "SELECT count(*) FROM artifact_lineage l JOIN active_assets a ON a.asset_id = l.asset_id"
            " WHERE l.error IS NOT NULL AND l.failed_at > :since"), {"since": now - h.FAILING_WINDOW}).scalar() or 0

    return h.processing_row(
        at=status.at if status else None, now=now,
        pause_state=status.state if status else "running",
        paused=[sw.title for sw in status.switches if sw.paused] if status else [],
        failing=_kept(tenant_id, "failing", COUNTS_EVERY_SEC, failing),
        unreadable=unreadable,
    )


def _ai(tenant_id: str, status: Any, now: datetime) -> h.Row:
    from src.server.database import get_control_session
    from src.server.models.control_plane import Tenant
    from src.server.repository.ai_machines import job_models, machines
    from src.shared.producers import PRODUCERS

    with get_control_session() as ctrl:
        every = [{"name": m.name, "jobs": list(m.jobs), "enabled": m.enabled, "online": m.online,
                  "error": m.status_error, "checked_at": m.checked_at} for m in machines(ctrl, tenant_id)]
        models = job_models(ctrl.get(Tenant, tenant_id))
    starved = set()
    eta = status.eta if status else None
    for n in (eta.not_counted if eta else []):
        p = PRODUCERS.get(n.artifact)
        if n.why == "no_machine" and p is not None and p.job:
            starved.add(p.job)
    live = bool(status and status.at and now - status.at < h.SCHEDULER_SILENT)
    return h.ai_row(machines=every, job_models=models, starved=starved, now=now, scheduler_live=live)


def _search(session: Session, tenant_id: str, now: datetime) -> h.Row:
    from src.server.config import get_settings
    from src.server.search import health as search_health
    from src.server.search.quickwit_client import QuickwitClient

    settings = get_settings()
    enabled = settings.quickwit_enabled
    quickwit, error, unsynced = None, "", 0
    if enabled:
        quickwit, error = _kept(tenant_id, "quickwit", QUICKWIT_EVERY_SEC,
                                lambda: QuickwitClient().tenant_index_state(tenant_id))
        if quickwit == "ok":
            unsynced = _kept(tenant_id, "unsynced", COUNTS_EVERY_SEC, lambda: session.execute(text(
                "SELECT count(*) FROM active_assets WHERE search_synced_at IS NULL")).scalar() or 0)
    return h.search_row(
        enabled=enabled, fallback_on=settings.quickwit_fallback_to_postgres, quickwit=quickwit,
        quickwit_error=error, last_fallback=search_health.last(session, search_health.FALLBACK),
        last_failure=search_health.last(session, search_health.FAILURE), unsynced=unsynced, now=now,
    )


def _disk(now: datetime) -> h.Row:
    from src.server.config import get_settings

    try:
        usage = shutil.disk_usage(get_settings().data_dir)
    except OSError as exc:
        return h.disk_row(free=None, total=None, now=now, error=_short(exc))
    return h.disk_row(free=usage.free, total=usage.total, now=now)

