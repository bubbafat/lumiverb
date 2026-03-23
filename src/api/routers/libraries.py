"""Libraries API: create and list libraries. All routes require tenant auth (middleware)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, field_validator
from sqlmodel import Session
from src.api.dependencies import get_tenant_session
from src.core.database import get_control_session
from src.core.io_utils import normalize_path_prefix
from src.core.utils import utcnow
from src.repository.control_plane import PublicLibraryRepository
from src.repository.tenant import AssetRepository, LibraryRepository, PathFilterRepository
from src.workers.quickwit import purge_library_from_quickwit

router = APIRouter(prefix="/v1/libraries", tags=["libraries"])


class CreateLibraryRequest(BaseModel):
    name: str
    root_path: str
    vision_model_id: str = "moondream"


class LibraryUpdateRequest(BaseModel):
    name: str | None = None
    vision_model_id: str | None = None
    is_public: bool | None = None

    @field_validator("vision_model_id")
    @classmethod
    def vision_model_id_not_empty(cls, v: str | None) -> str | None:
        if v is not None and not v:
            raise ValueError("vision_model_id must not be empty")
        return v


class LibraryResponse(BaseModel):
    library_id: str
    name: str
    root_path: str
    scan_status: str
    vision_model_id: str
    is_public: bool = False


class LibraryListItem(BaseModel):
    library_id: str
    name: str
    root_path: str
    scan_status: str
    last_scan_at: str | None
    status: str = "active"
    vision_model_id: str = ""
    is_public: bool = False


class EmptyTrashResponse(BaseModel):
    deleted: int


class DirectoryItem(BaseModel):
    name: str
    path: str
    asset_count: int


@router.post("", response_model=LibraryResponse)
def create_library(
    request: Request,
    body: CreateLibraryRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
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
    library = repo.create(name=body.name, root_path=body.root_path, vision_model_id=body.vision_model_id)
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id:
        path_filter_repo = PathFilterRepository(session)
        path_filter_repo.copy_defaults_to_library(tenant_id=tenant_id, library_id=library.library_id)
    return LibraryResponse(
        library_id=library.library_id,
        name=library.name,
        root_path=library.root_path,
        scan_status=library.scan_status,
        vision_model_id=library.vision_model_id,
        is_public=library.is_public,
    )


@router.get("", response_model=list[LibraryListItem])
def list_libraries(
    session: Annotated[Session, Depends(get_tenant_session)],
    include_trashed: Annotated[bool, Query(description="Include libraries with status=trashed")] = False,
) -> list[LibraryListItem]:
    """Return all libraries for the tenant with id, name, root_path, scan_status, last_scan_at, status."""
    repo = LibraryRepository(session)
    libraries = repo.list_all(include_trashed=include_trashed)
    return [
        LibraryListItem(
            library_id=lib.library_id,
            name=lib.name,
            root_path=lib.root_path,
            scan_status=lib.scan_status,
            last_scan_at=lib.last_scan_at.isoformat() if lib.last_scan_at else None,
            status=lib.status,
            vision_model_id=lib.vision_model_id,
            is_public=lib.is_public,
        )
        for lib in libraries
    ]


@router.post("/empty-trash", response_model=EmptyTrashResponse)
def empty_trash(
    session: Annotated[Session, Depends(get_tenant_session)],
) -> EmptyTrashResponse:
    """Hard delete all trashed libraries for this tenant. Returns count of libraries deleted."""
    repo = LibraryRepository(session)
    trashed = repo.get_trashed()
    deleted = 0
    for lib in trashed:
        purge_library_from_quickwit(lib.library_id)
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
    if getattr(request.state, "is_public_request", False) and not library.is_public:
        raise HTTPException(status_code=404, detail="Not found")
    return LibraryResponse(
        library_id=library.library_id,
        name=library.name,
        root_path=library.root_path,
        scan_status=library.scan_status,
        vision_model_id=library.vision_model_id,
        is_public=library.is_public,
    )


@router.patch("/{library_id}", response_model=LibraryResponse)
def update_library(
    library_id: str,
    request: Request,
    body: LibraryUpdateRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> LibraryResponse:
    """Update library name, vision_model_id, and/or is_public."""
    repo = LibraryRepository(session)
    library = repo.get_by_id(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    if body.vision_model_id is not None:
        library.vision_model_id = body.vision_model_id
    if body.name is not None:
        library.name = body.name
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
        scan_status=library.scan_status,
        vision_model_id=library.vision_model_id,
        is_public=library.is_public,
    )


@router.delete("/{library_id}", status_code=204)
def delete_library(
    library_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> None:
    """Soft delete: move library to trash (status=trashed), cancel pending/claimed jobs. Returns 409 if already trashed."""
    repo = LibraryRepository(session)
    library = repo.get_by_id(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
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
