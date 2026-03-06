"""Libraries API: create and list libraries. All routes require tenant auth (middleware)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.repository.tenant import LibraryRepository

router = APIRouter(prefix="/v1/libraries", tags=["libraries"])


class CreateLibraryRequest(BaseModel):
    name: str
    root_path: str


class LibraryResponse(BaseModel):
    library_id: str
    name: str
    root_path: str
    scan_status: str


class LibraryListItem(BaseModel):
    library_id: str
    name: str
    root_path: str
    scan_status: str
    last_scan_at: str | None


@router.post("", response_model=LibraryResponse)
def create_library(
    body: CreateLibraryRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> LibraryResponse:
    """
    Create a library. Name must be unique for this tenant.
    Returns 409 if a library with the same name already exists.
    """
    repo = LibraryRepository(session)
    existing = repo.get_by_name(body.name)
    if existing is not None:
        raise HTTPException(status_code=409, detail="A library with this name already exists")
    library = repo.create(name=body.name, root_path=body.root_path)
    return LibraryResponse(
        library_id=library.library_id,
        name=library.name,
        root_path=library.root_path,
        scan_status=library.scan_status,
    )


@router.get("", response_model=list[LibraryListItem])
def list_libraries(
    session: Annotated[Session, Depends(get_tenant_session)],
) -> list[LibraryListItem]:
    """Return all libraries for the tenant with id, name, root_path, scan_status, last_scan_at."""
    repo = LibraryRepository(session)
    libraries = repo.list_all()
    return [
        LibraryListItem(
            library_id=lib.library_id,
            name=lib.name,
            root_path=lib.root_path,
            scan_status=lib.scan_status,
            last_scan_at=lib.last_scan_at.isoformat() if lib.last_scan_at else None,
        )
        for lib in libraries
    ]
