"""Libraries API: create and list libraries. All routes require tenant auth (middleware)."""

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlmodel import Session

from src.server.api.dependencies import (
    checked_root_path,
    get_current_user_id,
    get_tenant_session,
    require_editor,
    require_folder_chosen_here,
    require_signed_in,
    require_tenant_admin,
)
from src.server.api.errors import ConflictError, DecisionRequiredError, GoneError
from src.server.api.limits import MAX_IDS, require_scope
from src.server.database import get_control_session
from src.server.repository import lineage
from src.server.repository.control_plane import PublicLibraryRepository
from src.server.repository.assets import AssetRepository
from src.server.repository.libraries import LibraryRepository, PathFilterRepository
from src.shared.io_utils import normalize_path_prefix
from src.shared.readers import SERVER, read_by_server

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/libraries", tags=["libraries"])


class CreateLibraryRequest(BaseModel):
    name: str
    # As its reader sees it.
    root_path: str
    is_public: bool | None = None
    # Who reads its files: "server" (this server), or a registered device's id (/v1/devices).
    reader: str = SERVER


class ReaderChoice(BaseModel):
    # "server" (this server), or a registered device's id (/v1/devices).
    reader: str
    # The folder as the new reader sees it; left out, the root stays as it is.
    root_path: str | None = None


def require_admin_for(request: Request, body: BaseModel, fields: set[str]) -> None:
    """Library fields only a tenant admin may change (is_public and root_path):
    403 for anyone else who sends them. The one place to swap in a per-library
    admin role."""
    if fields & body.model_fields_set:
        require_tenant_admin(request)


class LibraryUpdateRequest(BaseModel):
    name: str | None = None
    root_path: str | None = None
    is_public: bool | None = None


class LibraryResponse(BaseModel):
    library_id: str
    name: str
    root_path: str
    is_public: bool = False
    cover_asset_id: str | None = None
    # Who reads its files (src/shared/readers.py): "server", this server, or a device's id.
    reader: str = SERVER
    # The device's name (Devices API), for people; None for this server.
    reader_name: str | None = None


class CheckedIn(BaseModel):
    """A library reader's latest check-in (ADR-018 phase 4, src/server/check_ins.py)."""

    client: str  # "this server", a device's name
    version: str
    state: str  # scanning | idle | paused | away (its storage can't be reached from the reader)
    waiting_files: int
    waiting_bytes: int
    last_full_scan_at: str | None
    checked_in_at: str


class LibraryListItem(BaseModel):
    library_id: str
    name: str
    root_path: str
    last_scan_at: str | None
    status: str = "active"
    is_public: bool = False
    cover_asset_id: str | None = None
    # In the trash since (status "trashed"): its trash days count from here.
    trashed_at: str | None = None
    # The counter GET /{library_id}/revision reports: bumped whenever the
    # library's clips change, so a page across libraries can follow them all.
    revision: int = 0
    # Who reads its files (src/shared/readers.py): "server", this server, or a device's id.
    reader: str = SERVER
    # The device's name (Devices API), for people; None for this server.
    reader_name: str | None = None
    # Its own Scans pause (ADR-018 phase 1): no new or changed files are
    # found in it. Pause all isn't said here (GET /v1/producers/queue says
    # each library's with it, and who paused it).
    scans_paused: bool = False
    # A scan of the whole library asked for (Scan now), until one started after it finishes.
    scan_requested_at: str | None = None
    # Its reader's latest check-in, none before the first.
    checked_in: CheckedIn | None = None


def checked_in_of(row: dict | None) -> CheckedIn | None:
    if row is None:
        return None
    full = row["last_full_scan_at"]
    return CheckedIn(client=row["client"], version=row["version"], state=row["state"],
                     waiting_files=row["waiting_files"], waiting_bytes=row["waiting_bytes"],
                     last_full_scan_at=full.isoformat() if full else None,
                     checked_in_at=row["checked_in_at"].isoformat())


class ScansPauseRequest(BaseModel):
    # Which libraries, or all: true for every one. Neither or both is a 400.
    library_ids: list[str] | None = Field(default=None, max_length=MAX_IDS)
    all: bool = False


class EmptyTrashResponse(BaseModel):
    deleted: int


class EmptyLibraryTrashRequest(BaseModel):
    # Which trashed libraries to delete for good, or all: true for every one
    # in the trash. Neither is a 400.
    library_ids: list[str] | None = Field(default=None, max_length=MAX_IDS)
    all: bool = False
    # Required when clips in those libraries are in projects: deleting them
    # for good takes them out of those projects.
    remove_from_projects: bool = False


class DirectoryItem(BaseModel):
    name: str
    path: str
    asset_count: int


@router.post("", response_model=LibraryResponse)
def create_library(
    request: Request,
    body: CreateLibraryRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_tenant_admin)],
) -> LibraryResponse:
    """
    Create a library. Admins only: a library is made with its folder, and
    choosing a library's folder is an admin's, as changing it is (PATCH).
    Name must be unique for this tenant.
    Returns 409 if a library with the same name already exists.
    New libraries inherit tenant path filter defaults at creation time.
    Read by this server unless reader names a device (404 device_not_found
    for one that isn't registered): its folder is then the device's, and
    chosen anywhere (it's never read here).
    """
    root_path = checked_root_path(body.root_path)
    _known_reader(session, body.reader)
    if body.reader == SERVER:
        require_folder_chosen_here(request)
    repo = LibraryRepository(session)
    existing = repo.get_by_name(body.name)
    if existing is not None:
        raise HTTPException(status_code=409, detail="A library with this name already exists")
    library = repo.create(name=body.name, root_path=root_path, reader=body.reader)
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id:
        path_filter_repo = PathFilterRepository(session)
        path_filter_repo.copy_defaults_to_library(tenant_id=tenant_id, library_id=library.library_id)
    if body.is_public:
        repo.update(library, is_public=True)
        with get_control_session() as ctrl_session:
            PublicLibraryRepository(ctrl_session).upsert(library.library_id, tenant_id)
    return LibraryResponse(
        library_id=library.library_id,
        name=library.name,
        root_path=library.root_path,
        is_public=library.is_public,
        reader=library.reader,
        reader_name=_reader_name(session, library.reader),
    )


@router.get("", response_model=list[LibraryListItem])
def list_libraries(
    session: Annotated[Session, Depends(get_tenant_session)],
    include_trashed: Annotated[bool, Query(description="Include libraries with status=trashed")] = False,
) -> list[LibraryListItem]:
    """Return all libraries for the tenant."""
    from src.server import check_ins

    from src.server.repository.devices import DeviceRepository

    repo = LibraryRepository(session)
    libraries = repo.list_all(include_trashed=include_trashed)
    seen = check_ins.latest(session)
    names = DeviceRepository(session).names()
    return [
        LibraryListItem(
            library_id=lib.library_id,
            name=lib.name,
            root_path=lib.root_path,
            last_scan_at=lib.last_scan_at.isoformat() if lib.last_scan_at else None,
            status=lib.status,
            is_public=lib.is_public,
            cover_asset_id=repo.resolve_cover(lib),
            trashed_at=lib.trashed_at.isoformat() if lib.trashed_at else None,
            revision=lib.revision,
            reader=lib.reader,
            reader_name=names.get(lib.reader),
            scans_paused=lib.scans_paused_at is not None,
            scan_requested_at=lib.scan_requested_at.isoformat() if lib.scan_requested_at else None,
            checked_in=checked_in_of(seen.get(lib.library_id)),
        )
        for lib in libraries
    ]


def _named_libraries(session: Session, body: ScansPauseRequest) -> list[str]:
    """The libraries a request names (404 for one that doesn't exist), or
    every one (all: true); neither or both is a 400 scope_required."""
    require_scope(body.library_ids is not None, body.all, "library_ids")
    known = [lib.library_id for lib in LibraryRepository(session).list_all(include_trashed=True)]
    if body.all:
        return known
    missing = sorted(set(body.library_ids or []) - set(known))
    if missing:
        raise HTTPException(status_code=404, detail=f"Library not found: {missing[0]}")
    return list(dict.fromkeys(body.library_ids or []))


@router.post("/scans/pause", status_code=204, dependencies=[Depends(require_tenant_admin)])
def pause_scans(
    body: ScansPauseRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> None:
    """Pause these libraries' scans, or every library's (admins; ADR-018
    phase 1): no new or changed files are found in them, so neither their
    thumbnails nor video previews are made; changes reported meanwhile
    wait; every other kind of work goes on. A scan running finishes. Again
    is fine: who paused first, and when, stays."""
    lineage.pause_scans(session, _named_libraries(session, body), by=user_id)
    session.commit()


@router.post("/scans/resume", status_code=204, dependencies=[Depends(require_tenant_admin)])
def resume_scans(body: ScansPauseRequest, session: Annotated[Session, Depends(get_tenant_session)]) -> None:
    """Scan these libraries again, or every library (admins). While Pause
    all is on, everything else stays paused, each on its own."""
    lineage.resume_scans(session, _named_libraries(session, body))
    session.commit()


class LibraryHealthItem(BaseModel):
    library_id: str
    healthy: bool
    pending: int


@router.get("/health", response_model=list[LibraryHealthItem], dependencies=[Depends(require_signed_in)])
def list_library_health(
    session: Annotated[Session, Depends(get_tenant_session)],
) -> list[LibraryHealthItem]:
    """Return one row per non-trashed library indicating whether any
    enrichment work is pending. `pending` is the number of assets that
    would show up on `repair --dry-run`; `healthy` is `pending == 0`.

    A single SQL query (GROUP BY library_id over `active_assets`) so the
    UI can render a green/orange dot per library without N+1ing the
    repair-summary endpoint. Trashed libraries are excluded — they have
    no work to do and shouldn't surface in the indicator either.
    """
    # Failures waiting their turn are pending too: not healthy. Libraries with
    # no clips (or all healthy) are listed too, so the UI can render a dot
    # for them: pending_counts lists only those with clips.
    repo = LibraryRepository(session)
    seen = repo.pending_counts()
    return [
        LibraryHealthItem(
            library_id=lib.library_id,
            pending=seen.get(lib.library_id, 0),
            healthy=seen.get(lib.library_id, 0) == 0,
        )
        for lib in repo.list_all(include_trashed=False)
    ]


@router.post("/empty-trash", response_model=EmptyTrashResponse)
def empty_trash(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_tenant_admin)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    body: EmptyLibraryTrashRequest,
) -> EmptyTrashResponse:
    """Delete trashed libraries for good: those in library_ids, or all of them
    (all: true; neither or both is a 400 scope_required).
    Admins only: deleting media for good right away is theirs (Robert's call,
    Oct 8); editors trash and restore, and the trash deletes on its own day.
    Returns how many. 409 in_projects, with the projects in details, when
    their clips are in projects and remove_from_projects isn't set."""
    require_scope(body.library_ids is not None, body.all, "library_ids")
    tenant_id = getattr(request.state, "tenant_id", None)
    repo = LibraryRepository(session)
    trashed = repo.get_trashed()
    if body.library_ids is not None:
        trashed = [lib for lib in trashed if lib.library_id in set(body.library_ids)]
    if trashed and not body.remove_from_projects:
        from src.server.api.routers.assets import project_usage_summary

        usage = project_usage_summary(session, user_id, library_ids=[lib.library_id for lib in trashed])
        if usage.assets_in_projects:
            n = usage.assets_in_projects
            raise DecisionRequiredError(
                "in_projects",
                f"{n} {'clip' if n == 1 else 'clips'} from the trashed libraries "
                f"{'is' if n == 1 else 'are'} in projects; deleting them for good removes them from "
                "those projects. Send remove_from_projects: true to go ahead.",
                usage.model_dump(),
            )
    return EmptyTrashResponse(deleted=delete_libraries_for_good(session, tenant_id, trashed))


def delete_libraries_for_good(
    session: Session, tenant_id: str | None, libraries: list, *, before: datetime | None = None,
) -> int:
    """Delete trashed libraries with everything in them: rows, search documents
    and public pages, and once those are committed, their previews folder
    (storage/move.py forget_library). Returns how many.

    Each is re-checked under a row lock (still in the trash, since before
    `before`): one restored since it was listed is never deleted, and a
    restore arriving meanwhile waits, then finds it gone."""
    repo = LibraryRepository(session)
    deleted = 0
    # Read before anything commits or rolls back (both expire the objects).
    for library_id in [lib.library_id for lib in libraries]:
        if not repo.lock_trashed(library_id, before):
            session.rollback()
            continue
        repo.hard_delete(library_id)  # commits, releasing the lock
        deleted += 1
        # Trash cleared is_public, so any public row left behind goes regardless.
        with get_control_session() as ctrl_session:
            PublicLibraryRepository(ctrl_session).delete(library_id)
        if tenant_id:
            _forget_previews(tenant_id, library_id)
    return deleted


def _forget_previews(tenant_id: str, library_id: str) -> None:
    """A deleted library's previews: gone now, or (a move under way, a drive
    away) left for the daily cleanup to report."""
    from pathlib import Path

    from src.server.config import get_settings
    from src.server.storage import move

    try:
        move.forget_library(Path(get_settings().data_dir), tenant_id, library_id)
    except (move.MoveError, OSError, ValueError) as exc:
        logger.warning("Library %s deleted for good; its previews stay for now: %s", library_id, exc)


@router.get("/{library_id}", response_model=LibraryResponse)
def get_library(
    library_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> LibraryResponse:
    """Return a single library by id. Public libraries are accessible without auth."""
    repo = LibraryRepository(session)
    library = repo.get_by_id(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    is_public_request = getattr(request.state, "is_public_request", False)
    if is_public_request and not library.is_public:
        raise HTTPException(status_code=404, detail="Not found")
    return LibraryResponse(
        library_id=library.library_id,
        name=library.name,
        # Where the files are on the server isn't a visitor's business.
        root_path="" if is_public_request else library.root_path,
        is_public=library.is_public,
        cover_asset_id=repo.resolve_cover(library),
        reader=library.reader,
        reader_name=_reader_name(session, library.reader),
    )


@router.patch("/{library_id}", response_model=LibraryResponse)
def update_library(
    library_id: str,
    request: Request,
    body: LibraryUpdateRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> LibraryResponse:
    """Update library name, root_path and/or is_public. A folder is chosen
    on the library's reader: for this server's libraries, as on creating one
    (require_folder_chosen_here)."""
    require_admin_for(request, body, {"is_public", "root_path"})
    if body.root_path is not None:
        checked_root_path(body.root_path)
    repo = LibraryRepository(session)
    library = repo.get_by_id(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    if body.root_path is not None and read_by_server(library):
        require_folder_chosen_here(request)
    repo.update(library, name=body.name, root_path=body.root_path, is_public=body.is_public)

    # Maintain public_libraries control plane index
    if body.is_public is not None:
        tenant_id = request.state.tenant_id
        with get_control_session() as ctrl_session:
            pub_repo = PublicLibraryRepository(ctrl_session)
            if library.is_public:
                pub_repo.upsert(library_id, tenant_id)
            else:
                pub_repo.delete(library_id)

    return LibraryResponse(
        library_id=library.library_id,
        name=library.name,
        root_path=library.root_path,
        is_public=library.is_public,
        cover_asset_id=repo.resolve_cover(library),
        reader=library.reader,
        reader_name=_reader_name(session, library.reader),
    )


def _reader_name(session: Session, reader: str) -> str | None:
    """A device's name, for people; None for this server (or a device gone since)."""
    from src.server.repository.devices import DeviceRepository

    if reader == SERVER:
        return None
    device = DeviceRepository(session).get(reader)
    return device["name"] if device is not None else None


def _known_reader(session: Session, reader: str) -> None:
    """404 device_not_found unless the reader is this server or a registered device."""
    from src.server.api.errors import ApiError
    from src.server.repository.devices import DeviceRepository

    if reader != SERVER and DeviceRepository(session).get(reader) is None:
        raise ApiError(404, "device_not_found", "There's no such device: register it first (/v1/devices).")


@router.put("/{library_id}/reader", response_model=LibraryResponse, dependencies=[Depends(require_tenant_admin)])
def choose_reader(
    library_id: str,
    request: Request,
    body: ReaderChoice,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> LibraryResponse:
    """Who reads the library's files (ADR-018 decision 1; admins): this
    server or a registered device, never both at once. The new reader scans
    it and makes what needs its originals from its next check-in; the old
    one's scans of it are refused from now (409 not_the_reader). With
    root_path, the folder as the new reader sees it. Handed to this server,
    its folder is chosen here as on creating a library
    (require_folder_chosen_here): this server reads it from now. 404 for no
    such library, 404 device_not_found for a device that isn't registered,
    409 library_trashed."""
    root_path = checked_root_path(body.root_path) if body.root_path is not None else None
    repo = LibraryRepository(session)
    library = repo.get_by_id(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    if library.status == "trashed":
        raise ConflictError("library_trashed", "The library is in the trash.")
    _known_reader(session, body.reader)
    if body.reader == SERVER and (root_path is not None or library.reader != SERVER):
        require_folder_chosen_here(request)
    repo.update(library, root_path=root_path, reader=body.reader)
    return LibraryResponse(
        library_id=library.library_id,
        name=library.name,
        root_path=library.root_path,
        is_public=library.is_public,
        cover_asset_id=repo.resolve_cover(library),
        reader=library.reader,
        reader_name=_reader_name(session, library.reader),
    )


class DeleteLibraryRequest(BaseModel):
    # Required when any of the library's clips are in projects: in the trash
    # they're hidden there, and deleted for good with the library they leave them.
    remove_from_projects: bool = False


@router.delete("/{library_id}", status_code=204)
def delete_library(
    library_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    body: DeleteLibraryRequest | None = None,
) -> None:
    """Move a library to the trash with everything in it: restorable until
    it's deleted for good after the trash days. 409 if already trashed.

    409 in_projects, with the projects in details, when any of its clips are
    in projects and remove_from_projects isn't set.
    """
    repo = LibraryRepository(session)
    library = repo.get_by_id(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    if library.status == "trashed":
        raise HTTPException(status_code=409, detail="Library is already in trash")
    if not (body and body.remove_from_projects):
        from src.server.api.routers.assets import project_usage_summary

        usage = project_usage_summary(session, user_id, library_ids=[library_id])
        if usage.assets_in_projects:
            n = usage.assets_in_projects
            raise DecisionRequiredError(
                "in_projects",
                f"{n} {'clip' if n == 1 else 'clips'} in this library {'is' if n == 1 else 'are'} in projects. "
                "In the trash they're hidden there; deleted for good with the library, they leave those "
                "projects. Send remove_from_projects: true to go ahead.",
                usage.model_dump(),
            )
    was_public = library.is_public
    try:
        repo.trash(library_id)
    except ValueError as e:
        if "already trashed" in str(e):
            raise HTTPException(status_code=409, detail="Library is already in trash") from e
        raise
    if was_public:
        with get_control_session() as ctrl_session:
            PublicLibraryRepository(ctrl_session).delete(library_id)


@router.post("/{library_id}/restore", response_model=LibraryResponse)
def restore_library(
    library_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> LibraryResponse:
    """Take a library out of the trash with the clips that went with it.
    Clips trashed or archived before it went stay as they were. It comes back
    private: a public page doesn't reappear without a person saying so."""
    repo = LibraryRepository(session)
    library = repo.get_by_id(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    if library.status != "trashed":
        raise ConflictError("not_trashed", "This library isn't in the trash.")
    try:
        library = repo.restore(library_id)
    except ValueError as e:
        # Deleted for good (or restored) while this waited for the purge holding it.
        if repo.get_by_id(library_id) is None:
            raise HTTPException(status_code=404, detail="Library not found") from e
        raise ConflictError("not_trashed", "This library isn't in the trash.") from e
    repo.bump_revision(library_id)
    return LibraryResponse(library_id=library.library_id, name=library.name,
                           root_path=library.root_path, is_public=library.is_public, reader=library.reader,
                           reader_name=_reader_name(session, library.reader))


@router.get("/{library_id}/ignored-paths", dependencies=[Depends(require_signed_in)])
def page_ignored_paths(library_id: str) -> None:
    """Retired (410 gone): a scan session skips these files on the server
    (src/server/scans.py REMOVED). Signed in only, as it always was."""
    raise GoneError(
        "GET /v1/libraries/{library_id}/ignored-paths is retired: a scan session skips the files a person "
        "trashed, archived or deleted for good on the server (ADR-018), so no client needs the list.",
        ["POST /v1/libraries/{library_id}/scans"],
    )


@router.delete("/{library_id}/ignored-paths", dependencies=[Depends(require_editor)])
def unignore_paths(library_id: str) -> None:
    """Retired (410 gone): a scan brings back the files a person removed when
    asked (force_add: `lumiverb scan --force-add`). An editor's, as it was."""
    raise GoneError(
        "DELETE /v1/libraries/{library_id}/ignored-paths is retired: a scan started with force_add "
        "(`lumiverb scan --force-add`) adds the files a person removed, and forgets those deleted for good.",
        ["POST /v1/libraries/{library_id}/scans"],
    )


@router.get("/{library_id}/directories", response_model=list[DirectoryItem])
def list_directories(
    library_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    parent: str = "",
) -> list[DirectoryItem]:
    """
    Return immediate child directories under the given parent path for a library.

    The directory tree is derived from asset rel_path values where status != 'deleted'.
    """
    # Basic path traversal protection
    if parent and any(part == ".." for part in parent.split("/")):
        raise HTTPException(status_code=400, detail="Invalid parent; path traversal not allowed")

    lib_repo = LibraryRepository(session)
    library = lib_repo.get_by_id(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    if getattr(request.state, "is_public_request", False) and not library.is_public:
        raise HTTPException(status_code=404, detail="Not found")

    # Normalize parent but treat empty string as root
    parent_norm = ""
    if parent:
        norm = normalize_path_prefix(parent)
        parent_norm = norm or ""

    asset_repo = AssetRepository(session)
    rel_paths = asset_repo.list_rel_paths_for_library_non_deleted(library_id)

    # Aggregate asset counts for each directory path
    dir_counts: dict[str, int] = {}
    for rel_path in rel_paths:
        parts = rel_path.split("/")
        if len(parts) <= 1:
            # Asset at library root; contributes to no subdirectories
            continue
        # For a/b/c.jpg -> directories: "a", "a/b"
        for depth in range(1, len(parts)):
            dir_path = "/".join(parts[:depth])
            dir_counts[dir_path] = dir_counts.get(dir_path, 0) + 1

    # Compute immediate children for the requested parent
    items: list[DirectoryItem] = []
    for dir_path, count in dir_counts.items():
        if "/" in dir_path:
            parent_of_dir, name = dir_path.rsplit("/", 1)
        else:
            parent_of_dir, name = "", dir_path
        if parent_of_dir == parent_norm:
            items.append(
                DirectoryItem(
                    name=name,
                    path=dir_path,
                    asset_count=count,
                )
            )

    items.sort(key=lambda d: d.name)
    return items


class LibraryRevisionResponse(BaseModel):
    library_id: str
    revision: int
    asset_count: int


@router.get("/{library_id}/revision", response_model=LibraryRevisionResponse)
def get_library_revision(
    library_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> LibraryRevisionResponse:
    """Lightweight endpoint for UI polling. Returns the library revision counter
    and asset count. Clients compare revision to detect changes without
    re-fetching full asset pages."""
    lib_repo = LibraryRepository(session)
    library = lib_repo.get_by_id(library_id)
    if library is None or (getattr(request.state, "is_public_request", False) and not library.is_public):
        raise HTTPException(status_code=404, detail="Library not found")
    asset_count = AssetRepository(session).count_by_library(library_id)
    return LibraryRevisionResponse(
        library_id=library_id,
        revision=library.revision,
        asset_count=asset_count,
    )


# -- Where its previews are -------------------------------------------------------


class LibraryPreviews(BaseModel):
    library_id: str
    folder: str  # where its previews are
    own: bool  # a folder of its own (else the server's, Settings › Storage)
    server_folder: str
    available: bool  # its drive is plugged in
    write_error: str | None = None  # Lumiverb can't write there now: why (src/server/write_check.py)
    free: int | None
    total: int | None
    # On the drive its originals are on: unplugged, it takes its previews too.
    # None when its originals aren't reachable here.
    same_drive_as_originals: bool | None
    moving: dict | None  # this library's move under way (GET /v1/system/storage's moving)
    changeable_here: bool  # moved only on the machine itself


class LibraryPreviewsChange(BaseModel):
    folder: str | None  # None: back to the server's previews folder
    move: bool = False  # the user saw what moves (the 409 move_previews) and said yes
    make_folder: bool = False  # it isn't there: the user said to make it (the 409 make_folder)


SAME_DRIVE = ("That's the drive this library's originals are on: when it's unplugged, its previews go too, "
              "and the library can't be browsed.")


# How long the originals' drive gets to answer: a network share whose server
# sleeps (prod's CIFS mount of the Mac's drives) can block a stat for minutes,
# and this runs in the request.
ORIGINALS_TIMEOUT_SEC = 2.0


def _originals_device(library) -> int | None:
    """The drive the library's originals are on here, or None when that
    isn't known now: no root map entry, not there, or not answering within
    ORIGINALS_TIMEOUT_SEC (src/processing/originals.py: the probe and the
    drive's look each in a thread)."""
    from src.processing.originals import reach

    try:
        originals = reach({"library_id": library.library_id, "name": library.name,
                           "root_path": library.root_path}, timeout=ORIGINALS_TIMEOUT_SEC)
    except Exception:  # noqa: BLE001 — a root map it can't read: not known here
        return None
    return None if originals is None else originals.device(timeout=ORIGINALS_TIMEOUT_SEC)


def _same_device(folder, device: int | None) -> bool | None:
    if device is None:
        return None
    try:
        return folder.stat().st_dev == device
    except OSError:
        return None


def _library_previews(request: Request, library) -> LibraryPreviews:
    import shutil
    from pathlib import Path

    from src.server import write_check
    from src.server.api.client_address import on_this_machine
    from src.server.config import get_settings
    from src.server.storage import move
    from src.shared import previews_location

    data = Path(get_settings().data_dir).absolute()
    location = previews_location.read(data)
    folder = location.root_for(library.library_id)
    available = location.available(data, folder)
    free = total = None
    if available:
        try:
            usage = shutil.disk_usage(folder)
            free, total = usage.free, usage.total
        except OSError:
            pass
    originals = _originals_device(library)
    moving = None
    if library.library_id in location.libraries_moving_from:
        state = move.read_state(data)
        moving = {k: state.get(k) for k in ("from_folder", "to_folder", "done_files", "total_files", "done_bytes",
                                            "total_bytes", "error", "started_at")}
        moving["running"] = move.running(data)
    return LibraryPreviews(
        library_id=library.library_id, folder=str(folder), own=library.library_id in location.libraries,
        server_folder=str(location.previews), available=available,
        write_error=write_check.why_not(folder) if available else None, free=free, total=total,
        same_drive_as_originals=_same_device(folder, originals),
        moving=moving, changeable_here=on_this_machine(request),
    )


@router.get("/{library_id}/previews", response_model=LibraryPreviews)
def get_library_previews(
    library_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_tenant_admin)],
) -> LibraryPreviews:
    """Where the library's previews are: the server's folder, or one of its own."""
    library = LibraryRepository(session).get_by_id(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    return _library_previews(request, library)


@router.put("/{library_id}/previews", response_model=LibraryPreviews, status_code=202)
def move_library_previews(
    library_id: str,
    body: LibraryPreviewsChange,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_tenant_admin)],
) -> LibraryPreviews:
    """Moves the library's previews to a folder of its own (or back to the
    server's), on the machine itself. Without move: true, a 409 says what
    moves (and whether it's the drive its originals are on) for the user to
    confirm; with nothing to move or say, they're there at once. A folder
    that isn't there: a 409 asks to make it first. Lumiverb keeps working
    meanwhile."""
    import shutil
    from pathlib import Path

    from src.server.api import new_folder
    from src.server.api.client_address import on_this_machine
    from src.server.api.errors import ApiError
    from src.server.config import get_settings
    from src.server.storage import move
    from src.shared import previews_location

    library = LibraryRepository(session).get_by_id(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    if not on_this_machine(request):
        raise ApiError(403, "storage_elsewhere", "Move the previews on the computer Lumiverb is installed on.")
    data = Path(get_settings().data_dir).absolute()
    if previews_location.read(data).moving:
        raise ApiError(409, "move_running", "The previews are being moved already: wait for that to finish.")
    try:
        with new_folder.made_if_asked(body.folder, body.make_folder, data):
            old, chosen, need = move.plan(data, body.folder, library_id=library_id)
            same = not body.move and _same_device(chosen, _originals_device(library)) is True
            if (need.files or same) and not body.move:
                said = (f"Moves {library.name}'s {need.files:,} previews ({move.size_text(need.bytes)}) from {old} "
                        f"to {chosen}. Lumiverb keeps working meanwhile; previews not moved yet show from where "
                        "they are." if need.files else f"{library.name}'s previews go to {chosen} from now on.")
                raise DecisionRequiredError(
                    "move_previews", said + (f" {SAME_DRIVE}" if same else ""),
                    {"from": str(old), "to": str(chosen), "files": need.files, "bytes": need.bytes,
                     "free": shutil.disk_usage(chosen).free, "same_drive_as_originals": same},
                )
            move.start(data, body.folder, library_id=library_id)
    except move.MoveError as exc:
        raise ApiError(400, "folder_not_usable", str(exc)) from None
    move.settled(data)
    return _library_previews(request, library)
