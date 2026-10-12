"""System health for the Admin page (GET /v1/system/health), whether
other devices on the network may use a Mac's Lumiverb (/v1/system/network,
Settings › Network), and the databases' backups (/v1/system/backups,
Settings › Backups).

Seven rows (website, processing, AI machines, search, storage, disk, backups), each
green, yellow or red with a one-line reason and where to fix it; the rules
are src/server/system_health.py. Admins see it (the Admin page, and its
link's dot); nobody else, a public page's visitor least of all. The web page asks every 10 seconds, so what costs more
than a query (asking Quickwit, counting clips) is kept for a little while
per worker.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import Session

from src.server import system_health as h
from src.server.api.dependencies import get_tenant_session, require_signed_in, require_tenant_admin
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
    key: str  # website | processing | ai | search | storage | disk | backups
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


@router.get("/health", response_model=SystemHealth,
            dependencies=[Depends(require_signed_in), Depends(require_tenant_admin)])
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
    rows.append(_row("backups", "Backups", lambda: _backups_row(now)))

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
    return h.disk_row(free=usage.free, total=usage.total, now=now, previews=_previews_disk(),
                      unwritable=_previews_unwritable())


def _previews_unwritable() -> list[str]:
    """Why Lumiverb can't write to the previews folder or a library's own,
    of those whose drive is there (an unplugged one is said already)."""
    from src.server import write_check
    from src.server.config import get_settings
    from src.shared import previews_location

    data = Path(get_settings().data_dir).absolute()
    location = previews_location.read(data)
    folders = [location.previews, *location.libraries.values()]
    return [why for folder in dict.fromkeys(folders)
            if location.available(data, folder) and (why := write_check.why_not(folder)) is not None]


def _previews_disk() -> dict | None:
    """The previews' folder for the disk row, when it isn't DATA_DIR or a move runs."""
    from src.server.config import get_settings
    from src.server.storage import move
    from src.shared import previews_location

    data = Path(get_settings().data_dir).absolute()
    location = previews_location.read(data)
    if location.previews == data and not location.moving and not location.libraries:
        return None
    out: dict = {"folder": str(location.previews), "available": location.available(data),
                 "same_disk": move.same_disk(location.previews, data), "moving": None,
                 "missing": sorted({str(root) for root in location.libraries.values()
                                    if not location.available(data, root)})}
    if location.moving:
        state = move.read_state(data)
        total = int(state.get("total_bytes") or 0)
        out["moving"] = min(1.0, int(state.get("done_bytes") or 0) / total) if total else 0.0
    if out["available"]:
        with contextlib.suppress(OSError):
            usage = shutil.disk_usage(location.previews)
            out["free"], out["total"] = usage.free, usage.total
    return out



def _backups_row(now: datetime) -> h.Row:
    from src.server import backups
    from src.server.config import get_settings

    data = Path(get_settings().data_dir)
    made = backups.list_backups(backups.folder(data))
    state = backups.read_state(data)
    return h.backups_row(
        newest=made[0].made_at if made else None, newest_bytes=made[0].bytes if made else 0,
        last_error=state.get("last_error"), last_failed_at=backups.parse_time(state.get("last_failed_at")),
        running=state.get("running") if backups.running(data) else None, folder_error=_backups_unwritable(data),
        now=now,
    )


def _backups_unwritable(data: Path) -> str | None:
    """Why Lumiverb can't write to the backups folder; the default one isn't
    there until the first backup makes it."""
    from src.server import backups, write_check

    folder = backups.folder(data)
    if folder == backups.default_folder(data) and not folder.is_dir():
        return None
    return write_check.why_not(folder)


class Network(BaseModel):
    # This install's own nginx (lumiverb-server run's): where it listens is
    # changed here. An nginx set up otherwise (a Linux install's) isn't.
    available: bool
    other_devices: bool  # may use it, or this machine only
    addresses: list[str]  # where other devices reach it


class NetworkChange(BaseModel):
    other_devices: bool


NOT_HERE = "Lumiverb doesn't run this server's web server, so who can reach it is set up where it's installed."
ELSEWHERE = "Change this on the computer Lumiverb is installed on."


def _network() -> Network:
    from src.server.config import get_settings
    from src.server.lifecycle import network

    settings = get_settings()
    data = Path(settings.data_dir)
    if settings.web_port <= 0 or not network.managed(data, settings.nginx_config):
        return Network(available=False, other_devices=False, addresses=[])
    return Network(available=True, other_devices=network.other_devices(data),
                   addresses=network.addresses(settings.web_port))


@router.get("/network", response_model=Network, dependencies=[Depends(require_signed_in)])
def get_network() -> Network:
    """Whether other devices on the network may use it, and where they reach it."""
    return _network()


@router.put("/network", response_model=Network, dependencies=[Depends(require_tenant_admin)])
def change_network(body: NetworkChange, request: Request) -> Network:
    """An admin's choice, on the machine itself (as for choosing a library's
    folder): run applies it in a few seconds, restarting nginx."""
    from src.server.api.client_address import on_this_machine
    from src.server.api.errors import ApiError
    from src.server.config import get_settings
    from src.server.lifecycle import network

    if not _network().available:
        raise ApiError(409, "network_not_here", NOT_HERE)
    if not on_this_machine(request):
        raise ApiError(403, "network_elsewhere", ELSEWHERE)
    network.set_other_devices(Path(get_settings().data_dir), body.other_devices)
    logger.info("Other devices on the network %s (an admin's choice)", "may use it" if body.other_devices else "can't use it")
    return _network()


class BackupItem(BaseModel):
    name: str
    made_at: datetime
    kind: str  # nightly | now | before-upgrade | before-restore
    bytes: int
    accounts: int
    restore: list[str]  # the commands that put it back, in order, run on this machine


class BackupRunning(BaseModel):
    kind: str
    started_at: datetime | None = None
    done: int = 0
    of: int = 0


class Backups(BaseModel):
    folder: str
    folder_write_error: str | None = None  # Lumiverb can't write there now: why (src/server/write_check.py)
    default_folder: str
    changeable_here: bool  # the folder is chosen on the machine itself
    running: BackupRunning | None
    last_error: str | None
    last_failed_at: datetime | None
    next_at: datetime  # when upkeep makes the nightly one next (now: within minutes)
    backups: list[BackupItem]


class BackupFolderChange(BaseModel):
    folder: str | None  # None: back to the default
    make_folder: bool = False  # it isn't there: the user said to make it (the 409 make_folder)


BACKUPS_ELSEWHERE = "Choose the backups folder on the computer Lumiverb is installed on."


def restore_commands(path: Path) -> list[str]:
    """How to put a backup back: Lumiverb stopped, restore, started. A Mac's
    service knows its env file (lumiverb-server run passes it); a Linux
    install's is deploy-vps.sh's."""
    import os
    import shlex
    import sys

    env_file = os.environ.get("LUMIVERB_ENV_FILE")
    if env_file:
        return ["brew services stop lumiverb",
                f"lumiverb-server restore --env {shlex.quote(env_file)} {shlex.quote(str(path))}",
                "brew services start lumiverb"]
    return ["sudo systemctl stop lumiverb-scheduler lumiverb-api",
            f"sudo {shlex.quote(sys.prefix)}/bin/lumiverb-server restore --env /etc/lumiverb/env {shlex.quote(str(path))}",
            "sudo systemctl start lumiverb-api lumiverb-scheduler"]


def _backups(request: Request) -> Backups:
    from src.server import backups
    from src.server.api.client_address import on_this_machine
    from src.server.config import get_settings

    data = Path(get_settings().data_dir)
    state = backups.read_state(data)
    running = state.get("running") if backups.running(data) else None
    made = backups.list_backups(backups.folder(data))
    failed = backups.parse_time(state.get("last_failed_at"))
    return Backups(
        folder=str(backups.folder(data)), folder_write_error=_backups_unwritable(data),
        default_folder=str(backups.default_folder(data)),
        changeable_here=on_this_machine(request),
        running=BackupRunning(**running) if isinstance(running, dict) else None,
        last_error=state.get("last_error"), last_failed_at=failed,
        next_at=backups.next_at(made[0].made_at if made else None, utcnow(), failed),
        backups=[BackupItem(name=b.name, made_at=b.made_at, kind=b.kind, bytes=b.bytes, accounts=b.accounts,
                            restore=restore_commands(b.path))
                 for b in made],
    )


@router.get("/backups", response_model=Backups, dependencies=[Depends(require_signed_in), Depends(require_tenant_admin)])
def get_backups(request: Request) -> Backups:
    """The backups made, where they go, and when the next one is."""
    return _backups(request)


@router.post("/backups", response_model=Backups, status_code=202,
             dependencies=[Depends(require_signed_in), Depends(require_tenant_admin)])
def back_up_now(request: Request) -> Backups:
    """Starts a backup now (Back up now); it runs in the background."""
    import os

    from src.server import backups
    from src.server.api.errors import ApiError
    from src.server.config import get_settings

    settings = get_settings()
    data = Path(settings.data_dir)
    if backups.running(data):
        raise ApiError(409, "backup_running", "A backup is being made already.")
    backups.start(data, settings.control_plane_database_url, "now", pg_bin=os.environ.get("PG_BIN") or None)
    logger.info("Backup started (an admin's Back up now)")
    for _ in range(20):  # it says so once it has the lock: the answer shows it running
        if backups.running(data):
            break
        time.sleep(0.05)
    return _backups(request)


@router.put("/backups/folder", response_model=Backups,
            dependencies=[Depends(require_signed_in), Depends(require_tenant_admin)])
def change_backups_folder(body: BackupFolderChange, request: Request) -> Backups:
    """An admin's choice, on the machine itself (as for a library's folder):
    backups go there from now on; those made already stay where they are."""
    from src.server import backups
    from src.server.api import new_folder
    from src.server.api.client_address import on_this_machine
    from src.server.api.errors import ApiError
    from src.server.config import get_settings

    if not on_this_machine(request):
        raise ApiError(403, "backups_elsewhere", BACKUPS_ELSEWHERE)
    data = Path(get_settings().data_dir)
    try:
        with new_folder.made_if_asked(body.folder, body.make_folder, data):
            chosen = backups.set_folder(data, body.folder)
    except backups.BackupError as exc:
        raise ApiError(400, "folder_not_usable", str(exc)) from None
    logger.info("Backups go to %s (an admin's choice)", chosen)
    return _backups(request)


class LibraryFolder(BaseModel):
    library_id: str
    name: str
    folder: str
    available: bool  # its drive is plugged in
    write_error: str | None = None  # Lumiverb can't write there now: why


class StorageMove(BaseModel):
    library_id: str | None = None  # one library's previews (else the server's)
    library_name: str | None = None
    from_folder: str
    to_folder: str
    done_files: int
    total_files: int
    done_bytes: int
    total_bytes: int
    running: bool
    error: str | None  # why it stopped; it's tried again (upkeep, 15 minutes later) or by hand
    started_at: datetime | None


class Storage(BaseModel):
    previews: str  # the folder the previews are in
    data: str  # DATA_DIR: the database, the search index, backups, caches and the AI models (they don't move)
    previews_available: bool  # its drive is plugged in
    previews_write_error: str | None = None  # Lumiverb can't write there now: why (src/server/write_check.py)
    previews_free: int | None
    previews_total: int | None
    data_free: int | None
    data_total: int | None
    same_disk: bool
    moving: StorageMove | None
    changeable_here: bool  # moved only on the machine itself
    # The account's libraries with a folder of their own (their settings).
    libraries: list[LibraryFolder] = []


class PreviewsChange(BaseModel):
    folder: str | None  # None: back to the data folder
    move: bool = False  # the user saw what moves (the 409 move_previews) and said yes
    make_folder: bool = False  # it isn't there: the user said to make it (the 409 make_folder)


STORAGE_ELSEWHERE = "Move the previews on the computer Lumiverb is installed on."


def _storage(request: Request, session: Session) -> Storage:
    from src.server import write_check
    from src.server.api.client_address import on_this_machine
    from src.server.config import get_settings
    from src.server.repository.tenant import LibraryRepository
    from src.server.storage import move
    from src.shared import previews_location

    data = Path(get_settings().data_dir).absolute()
    location = previews_location.read(data)

    def unwritable(path: Path, available: bool) -> str | None:
        return write_check.why_not(path) if available else None

    def usage(path: Path) -> tuple[int | None, int | None]:
        try:
            u = shutil.disk_usage(path)
            return u.free, u.total
        except OSError:
            return None, None

    available = location.available(data)
    previews_free, previews_total = usage(location.previews) if available else (None, None)
    data_free, data_total = usage(data)
    names = {lib.library_id: lib.name for lib in LibraryRepository(session).list_all()}
    moving = None
    if location.moving:
        state = move.read_state(data)
        library_id = state.get("library_id")
        if library_id is not None and library_id not in names:
            library_id = None  # another account's library: said as the server's previews moving
        moving = StorageMove(
            library_id=library_id, library_name=names.get(library_id) if library_id else None,
            from_folder=str(state.get("from_folder") or location.moving_from or ""),
            to_folder=str(state.get("to_folder") or location.previews),
            done_files=int(state.get("done_files") or 0), total_files=int(state.get("total_files") or 0),
            done_bytes=int(state.get("done_bytes") or 0), total_bytes=int(state.get("total_bytes") or 0),
            running=move.running(data), error=state.get("error"),
            started_at=state.get("started_at"),
        )
    own = []
    for lib, root in sorted(location.libraries.items(), key=lambda i: names.get(i[0], "")):
        if lib in names:
            there = location.available(data, root)
            own.append(LibraryFolder(library_id=lib, name=names[lib], folder=str(root), available=there,
                                     write_error=unwritable(root, there)))
    return Storage(previews=str(location.previews), data=str(data), previews_available=available,
                   previews_write_error=unwritable(location.previews, available), previews_free=previews_free,
                   previews_total=previews_total, data_free=data_free, data_total=data_total,
                   same_disk=move.same_disk(location.previews, data), moving=moving,
                   changeable_here=on_this_machine(request), libraries=own)


@router.get("/storage", response_model=Storage, dependencies=[Depends(require_signed_in), Depends(require_tenant_admin)])
def get_storage_location(request: Request, session: Annotated[Session, Depends(get_tenant_session)]) -> Storage:
    """Where the previews are, how full their disk and the data disk are, and a move under way."""
    return _storage(request, session)


@router.put("/storage/previews", response_model=Storage, status_code=202,
            dependencies=[Depends(require_signed_in), Depends(require_tenant_admin)])
def move_previews(body: PreviewsChange, request: Request,
                  session: Annotated[Session, Depends(get_tenant_session)]) -> Storage:
    """Moves the previews to another folder (or back to the data folder), on
    the machine itself. Without move: true, a 409 says what moves (files,
    bytes, from, to, free there) for the user to confirm; with nothing to
    move, they're there at once. A folder that isn't there: a 409 asks to
    make it first. Lumiverb keeps working meanwhile; new files go to the
    new folder at once."""
    from src.server.api import new_folder
    from src.server.api.client_address import on_this_machine
    from src.server.api.errors import ApiError, DecisionRequiredError
    from src.server.config import get_settings
    from src.server.storage import move
    from src.shared import previews_location

    if not on_this_machine(request):
        raise ApiError(403, "storage_elsewhere", STORAGE_ELSEWHERE)
    data = Path(get_settings().data_dir).absolute()
    location = previews_location.read(data)
    if location.moving:
        raise ApiError(409, "move_running", "The previews are being moved already: wait for that to finish.")
    target = body.folder if body.folder is not None else str(data)
    try:
        with new_folder.made_if_asked(body.folder, body.make_folder, data):
            _, chosen, need = move.plan(data, body.folder)
            if need.files and not body.move:
                raise DecisionRequiredError(
                    "move_previews",
                    f"Moves {need.files:,} previews ({move.size_text(need.bytes)}) from {location.previews} to "
                    f"{chosen}. Lumiverb keeps working meanwhile; previews not moved yet show from where they are.",
                    {"from": str(location.previews), "to": str(chosen), "files": need.files, "bytes": need.bytes,
                     "free": shutil.disk_usage(chosen).free},
                )
            move.start(data, target)
    except move.MoveError as exc:
        raise ApiError(400, "folder_not_usable", str(exc)) from None
    logger.info("Moving the previews to %s (an admin's choice)", target)
    move.settled(data)
    return _storage(request, session)


@router.post("/storage/move", response_model=Storage, status_code=202,
             dependencies=[Depends(require_signed_in), Depends(require_tenant_admin)])
def go_on_moving(request: Request, session: Annotated[Session, Depends(get_tenant_session)]) -> Storage:
    """Try again: a move that stopped (its error said why) goes on now."""
    from src.server.api.errors import ApiError
    from src.server.config import get_settings
    from src.server.storage import move

    data = Path(get_settings().data_dir).absolute()
    if move.go_on(data) == "not moving":
        raise ApiError(409, "not_moving", "The previews aren't being moved.")
    for _ in range(20):  # the answer shows it running once it has the lock
        if move.running(data):
            break
        time.sleep(0.05)
    return _storage(request, session)
