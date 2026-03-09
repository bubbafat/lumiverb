"""Libraries API: create and list libraries. All routes require tenant auth (middleware)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.models.registry import VALID_MODEL_IDS
from src.repository.tenant import LibraryRepository, _utcnow
from src.workers.quickwit import purge_library_from_quickwit

router = APIRouter(prefix="/v1/libraries", tags=["libraries"])


class CreateLibraryRequest(BaseModel):
    name: str
    root_path: str
    vision_model_id: str = "moondream"


class LibraryUpdateRequest(BaseModel):
    name: str | None = None
    vision_model_id: str | None = None


class LibraryResponse(BaseModel):
    library_id: str
    name: str
    root_path: str
    scan_status: str
    vision_model_id: str


class LibraryListItem(BaseModel):
    library_id: str
    name: str
    root_path: str
    scan_status: str
    last_scan_at: str | None
    status: str = "active"
    vision_model_id: str = "moondream"


class EmptyTrashResponse(BaseModel):
    deleted: int


@router.post("", response_model=LibraryResponse)
def create_library(
    body: CreateLibraryRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> LibraryResponse:
    """
    Create a library. Name must be unique for this tenant.
    Returns 409 if a library with the same name already exists.
    """
    if body.vision_model_id not in VALID_MODEL_IDS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown vision_model_id. Valid: {sorted(VALID_MODEL_IDS)}",
        )
    repo = LibraryRepository(session)
    existing = repo.get_by_name(body.name)
    if existing is not None:
        raise HTTPException(status_code=409, detail="A library with this name already exists")
    library = repo.create(name=body.name, root_path=body.root_path, vision_model_id=body.vision_model_id)
    return LibraryResponse(
        library_id=library.library_id,
        name=library.name,
        root_path=library.root_path,
        scan_status=library.scan_status,
        vision_model_id=library.vision_model_id,
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
        repo.hard_delete(lib.library_id)
        deleted += 1
    return EmptyTrashResponse(deleted=deleted)


@router.patch("/{library_id}", response_model=LibraryResponse)
def update_library(
    library_id: str,
    body: LibraryUpdateRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> LibraryResponse:
    """Update library name and/or vision_model_id."""
    repo = LibraryRepository(session)
    library = repo.get_by_id(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    if body.vision_model_id is not None:
        if body.vision_model_id not in VALID_MODEL_IDS:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown vision_model_id. Valid: {sorted(VALID_MODEL_IDS)}",
            )
        library.vision_model_id = body.vision_model_id
    if body.name is not None:
        library.name = body.name
    library.updated_at = _utcnow()
    session.add(library)
    session.commit()
    session.refresh(library)
    return LibraryResponse(
        library_id=library.library_id,
        name=library.name,
        root_path=library.root_path,
        scan_status=library.scan_status,
        vision_model_id=library.vision_model_id,
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
    try:
        repo.trash(library_id)
    except ValueError as e:
        if "already trashed" in str(e):
            raise HTTPException(status_code=409, detail="Library is already in trash") from e
        raise
