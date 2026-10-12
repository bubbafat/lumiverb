"""Libraries API: create and list libraries. All routes require tenant auth (middleware)."""

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
from src.server.api.errors import ConflictError, DecisionRequiredError
from src.server.api.limits import MAX_IDS, MAX_PAGE, require_scope
from src.server.database import get_control_session
from src.server.repository.control_plane import PublicLibraryRepository
from src.server.repository.tenant import AssetRepository, LibraryRepository, PathFilterRepository
from src.server.search.quickwit import purge_library_from_quickwit
from src.shared.io_utils import normalize_path_prefix
from src.shared.utils import utcnow

router = APIRouter(prefix="/v1/libraries", tags=["libraries"])


class CreateLibraryRequest(BaseModel):
    name: str
    root_path: str
    is_public: bool | None = None


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


class IgnoredFileItem(BaseModel):
    sha256: str
    # Its size and time when a person removed it: a scanner hashes the file
    # at the path only when they differ.
    file_size: int | None = None
    file_mtime: datetime | None = None


class IgnoredPathItem(BaseModel):
    rel_path: str
    # "trashed": in the trash by the user's choice. "archived": a person
    # archived it. "emptied": the user emptied its trash; the file may still
    # be on disk.
    reason: str
    # The files skipped at this path; None when one's content isn't known
    # (then whatever is there). Another file at the path is a new clip.
    files: list[IgnoredFileItem] | None = None


class IgnoredPathPage(BaseModel):
    items: list[IgnoredPathItem]
    next_cursor: str | None


class UnignoreRequest(BaseModel):
    rel_paths: list[str]


class UnignoreResponse(BaseModel):
    removed: int


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
    """
    root_path = checked_root_path(body.root_path)
    require_folder_chosen_here(request)
    repo = LibraryRepository(session)
    existing = repo.get_by_name(body.name)
    if existing is not None:
        raise HTTPException(status_code=409, detail="A library with this name already exists")
    library = repo.create(name=body.name, root_path=root_path)
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id:
        path_filter_repo = PathFilterRepository(session)
        path_filter_repo.copy_defaults_to_library(tenant_id=tenant_id, library_id=library.library_id)
    if body.is_public:
        library.is_public = True
        session.add(library)
        session.commit()
        session.refresh(library)
        with get_control_session() as ctrl_session:
            PublicLibraryRepository(ctrl_session).upsert(library.library_id, tenant_id,
                                                         request.state.connection_string)
    return LibraryResponse(
        library_id=library.library_id,
        name=library.name,
        root_path=library.root_path,
        is_public=library.is_public,
    )


@router.get("", response_model=list[LibraryListItem])
def list_libraries(
    session: Annotated[Session, Depends(get_tenant_session)],
    include_trashed: Annotated[bool, Query(description="Include libraries with status=trashed")] = False,
) -> list[LibraryListItem]:
    """Return all libraries for the tenant."""
    repo = LibraryRepository(session)
    libraries = repo.list_all(include_trashed=include_trashed)
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
        )
        for lib in libraries
    ]


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
    from sqlalchemy import text

    from src.server.repository.lineage import LINEAGE_JOIN
    from src.server.repository.tenant import OUTSTANDING_CONDITIONS

    # Failures waiting their turn are pending too: not healthy.
    pending_clause = " OR ".join(
        f"({cond})" for cond in OUTSTANDING_CONDITIONS.values()
    )
    pending_clause = f"a.proxy_key IS NULL OR {pending_clause}"

    rows = session.execute(
        text(f"""
            SELECT
                a.library_id,
                COUNT(*) FILTER (WHERE {pending_clause}) AS pending
            FROM active_assets a
            {LINEAGE_JOIN}
            JOIN libraries l ON l.library_id = a.library_id
            WHERE l.status != 'trashed'
            GROUP BY a.library_id
        """)
    ).all()

    # Include libraries with zero assets (or all healthy) explicitly so
    # the UI can render a dot for them too. The GROUP BY above only
    # returns rows for libraries that have at least one asset.
    seen = {row.library_id: row.pending for row in rows}
    repo = LibraryRepository(session)
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
    and public pages. Their files go with the daily cleanup. Returns how many.

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
        purge_library_from_quickwit(library_id, tenant_id=tenant_id)
        # Trash cleared is_public, so any public row left behind goes regardless.
        with get_control_session() as ctrl_session:
            PublicLibraryRepository(ctrl_session).delete(library_id)
    return deleted


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
    )


@router.patch("/{library_id}", response_model=LibraryResponse)
def update_library(
    library_id: str,
    request: Request,
    body: LibraryUpdateRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> LibraryResponse:
    """Update library name, root_path and/or is_public."""
    require_admin_for(request, body, {"is_public", "root_path"})
    if body.root_path is not None:
        checked_root_path(body.root_path)
        require_folder_chosen_here(request)
    repo = LibraryRepository(session)
    library = repo.get_by_id(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    if body.name is not None:
        library.name = body.name
    if body.root_path is not None:
        library.root_path = body.root_path
    if body.is_public is not None:
        library.is_public = body.is_public
    library.updated_at = utcnow()
    session.add(library)
    session.commit()
    session.refresh(library)

    # Maintain public_libraries control plane index
    if body.is_public is not None:
        tenant_id = request.state.tenant_id
        connection_string = request.state.connection_string
        with get_control_session() as ctrl_session:
            pub_repo = PublicLibraryRepository(ctrl_session)
            if library.is_public:
                pub_repo.upsert(library_id, tenant_id, connection_string)
            else:
                pub_repo.delete(library_id)

    return LibraryResponse(
        library_id=library.library_id,
        name=library.name,
        root_path=library.root_path,
        is_public=library.is_public,
        cover_asset_id=repo.resolve_cover(library),
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
                           root_path=library.root_path, is_public=library.is_public)


@router.get("/{library_id}/ignored-paths", response_model=IgnoredPathPage, dependencies=[Depends(require_signed_in)])
def page_ignored_paths(
    library_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    after: str | None = None,
    limit: int = Query(default=500, ge=1, le=MAX_PAGE),
) -> IgnoredPathPage:
    """Files a scan must skip because a person trashed, archived or deleted
    them, by rel_path, with their contents.

    Lumiverb never deletes originals, so these files may still be on disk.
    Ingest refuses them with 409; another file at the path is a new clip.
    """
    if LibraryRepository(session).get_by_id(library_id) is None:
        raise HTTPException(status_code=404, detail="Library not found")
    rows = AssetRepository(session).page_ignored_paths(library_id, after=after, limit=limit)
    return IgnoredPathPage(
        items=[IgnoredPathItem(rel_path=p, reason=k, files=f) for p, k, f in rows],
        next_cursor=rows[-1][0] if len(rows) == limit else None,
    )


@router.delete("/{library_id}/ignored-paths", response_model=UnignoreResponse)
def unignore_paths(
    library_id: str,
    body: UnignoreRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> UnignoreResponse:
    """Forget emptied-trash records so the next scan ingests those files again.

    Assets still in the trash are restored with POST /v1/assets/{id}/restore instead.
    """
    removed = AssetRepository(session).unignore(library_id, body.rel_paths)
    return UnignoreResponse(removed=removed)


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
# A stat of the originals still stuck, per folder: while one is, no other starts
# (as roots.reachable_root does for its probe), so threads don't pile up.
_stats: dict[str, object] = {}


def _originals_device(library) -> int | None:
    """The device the library's originals are on here, or None when that
    isn't known now: no root map entry, not there, or not answering within
    ORIGINALS_TIMEOUT_SEC (roots.reachable_root's probe, in a thread)."""
    import os
    import threading

    from src.processing.roots import reachable_root

    try:
        root = reachable_root({"library_id": library.library_id, "name": library.name,
                               "root_path": library.root_path}, timeout=ORIGINALS_TIMEOUT_SEC)
    except Exception:  # noqa: BLE001 — a root map it can't read: not known here
        return None
    if root is None:
        return None
    stuck = _stats.get(str(root))
    if stuck is not None and stuck.is_alive():
        return None
    found: dict[str, int] = {}

    def stat() -> None:
        found["dev"] = os.stat(root).st_dev

    worker = threading.Thread(target=stat, name=f"originals-stat-{library.library_id}", daemon=True)
    _stats[str(root)] = worker
    worker.start()
    worker.join(ORIGINALS_TIMEOUT_SEC)
    return found.get("dev")


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
