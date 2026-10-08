"""Libraries API: create and list libraries. All routes require tenant auth (middleware)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlmodel import Session

from src.server.api.dependencies import (
    get_current_user_id,
    get_tenant_session,
    require_editor,
    require_signed_in,
)
from src.server.api.errors import ConflictError, DecisionRequiredError
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


class EmptyTrashResponse(BaseModel):
    deleted: int


class EmptyLibraryTrashRequest(BaseModel):
    # Which trashed libraries to delete for good; all of them when omitted.
    library_ids: list[str] | None = None
    # Required when clips in those libraries are in projects: deleting them
    # for good takes them out of those projects.
    remove_from_projects: bool = False


class IgnoredPathItem(BaseModel):
    rel_path: str
    # "trashed": in the trash by the user's choice. "emptied": the user
    # emptied its trash; the file may still be on disk.
    reason: str


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
    _: Annotated[None, Depends(require_editor)],
) -> LibraryResponse:
    """
    Create a library. Name must be unique for this tenant.
    Returns 409 if a library with the same name already exists.
    New libraries inherit tenant path filter defaults at creation time.
    """
    repo = LibraryRepository(session)
    existing = repo.get_by_name(body.name)
    if existing is not None:
        raise HTTPException(status_code=409, detail="A library with this name already exists")
    library = repo.create(name=body.name, root_path=body.root_path)
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id:
        path_filter_repo = PathFilterRepository(session)
        path_filter_repo.copy_defaults_to_library(tenant_id=tenant_id, library_id=library.library_id)
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

    from src.server.repository.tenant import MISSING_CONDITIONS

    pending_clause = " OR ".join(
        f"({cond})" for cond in MISSING_CONDITIONS.values()
    )
    pending_clause = f"a.proxy_key IS NULL OR {pending_clause}"

    rows = session.execute(
        text(f"""
            SELECT
                a.library_id,
                COUNT(*) FILTER (WHERE {pending_clause}) AS pending
            FROM active_assets a
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
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    body: EmptyLibraryTrashRequest | None = None,
) -> EmptyTrashResponse:
    """Delete trashed libraries for good: those in library_ids, or all of them.
    Returns how many. 409 in_projects, with the projects in details, when
    their clips are in projects and remove_from_projects isn't set."""
    tenant_id = getattr(request.state, "tenant_id", None)
    repo = LibraryRepository(session)
    trashed = repo.get_trashed()
    if body and body.library_ids is not None:
        trashed = [lib for lib in trashed if lib.library_id in set(body.library_ids)]
    if trashed and not (body and body.remove_from_projects):
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
    deleted = 0
    for lib in trashed:
        purge_library_from_quickwit(lib.library_id, tenant_id=tenant_id)
        if lib.is_public:
            with get_control_session() as ctrl_session:
                PublicLibraryRepository(ctrl_session).delete(lib.library_id)
        repo.hard_delete(lib.library_id)
        deleted += 1
    return EmptyTrashResponse(deleted=deleted)


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
    """Update library name and/or is_public."""
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
    library.is_public = False
    library = repo.restore(library_id)
    repo.bump_revision(library_id)
    return LibraryResponse(library_id=library.library_id, name=library.name,
                           root_path=library.root_path, is_public=library.is_public)


@router.get("/{library_id}/ignored-paths", response_model=IgnoredPathPage, dependencies=[Depends(require_signed_in)])
def page_ignored_paths(
    library_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    after: str | None = None,
    limit: int = 500,
) -> IgnoredPathPage:
    """Paths a scan must skip because the user trashed them, by rel_path.

    Lumiverb never deletes originals, so these files may still be on disk.
    Ingest refuses them with 409.
    """
    if LibraryRepository(session).get_by_id(library_id) is None:
        raise HTTPException(status_code=404, detail="Library not found")
    limit = max(1, min(limit, 1000))
    rows = AssetRepository(session).page_ignored_paths(library_id, after=after, limit=limit)
    return IgnoredPathPage(
        items=[IgnoredPathItem(rel_path=p, reason=k) for p, k in rows],
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
