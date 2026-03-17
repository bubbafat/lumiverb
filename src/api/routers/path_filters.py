"""Library path filters API. All routes require tenant auth + require_tenant_admin."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session, require_tenant_admin
from src.core.path_filter import validate_pattern
from src.repository.tenant import LibraryRepository, PathFilterRepository

router = APIRouter(prefix="/v1/libraries", tags=["path_filters"])


class LibraryFilterItem(BaseModel):
    filter_id: str
    pattern: str
    created_at: str


class LibraryFilterItemWithType(BaseModel):
    filter_id: str
    type: str
    pattern: str
    created_at: str


class LibraryFiltersResponse(BaseModel):
    includes: list[LibraryFilterItem]
    excludes: list[LibraryFilterItem]


class CreateLibraryFilterRequest(BaseModel):
    type: str  # "include" | "exclude"
    pattern: str


@router.get("/{library_id}/filters", response_model=LibraryFiltersResponse)
def list_library_filters(
    library_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_tenant_admin)],
) -> LibraryFiltersResponse:
    """Return include and exclude path filters for a library."""
    lib_repo = LibraryRepository(session)
    if lib_repo.get_by_id(library_id) is None:
        raise HTTPException(status_code=404, detail="Library not found")
    filter_repo = PathFilterRepository(session)
    raw = filter_repo.list_for_library(library_id)
    includes = [
        LibraryFilterItem(filter_id=f.filter_id, pattern=f.pattern, created_at=f.created_at.isoformat())
        for f in raw if f.type == "include"
    ]
    excludes = [
        LibraryFilterItem(filter_id=f.filter_id, pattern=f.pattern, created_at=f.created_at.isoformat())
        for f in raw if f.type == "exclude"
    ]
    return LibraryFiltersResponse(includes=includes, excludes=excludes)


@router.post("/{library_id}/filters", response_model=LibraryFilterItemWithType, status_code=201)
def create_library_filter(
    library_id: str,
    body: CreateLibraryFilterRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_tenant_admin)],
) -> LibraryFilterItemWithType:
    """Add a path filter to a library. Returns 400 if pattern invalid, 404 if library not found."""
    if body.type not in ("include", "exclude"):
        raise HTTPException(status_code=400, detail="type must be 'include' or 'exclude'")
    try:
        validate_pattern(body.pattern)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    lib_repo = LibraryRepository(session)
    if lib_repo.get_by_id(library_id) is None:
        raise HTTPException(status_code=404, detail="Library not found")
    filter_repo = PathFilterRepository(session)
    row = filter_repo.add_for_library(library_id=library_id, type=body.type, pattern=body.pattern)
    return LibraryFilterItemWithType(
        filter_id=row.filter_id,
        type=row.type,
        pattern=row.pattern,
        created_at=row.created_at.isoformat(),
    )


@router.delete("/{library_id}/filters/{filter_id}", status_code=204)
def delete_library_filter(
    library_id: str,
    filter_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_tenant_admin)],
) -> None:
    """Remove a path filter. Returns 404 if not found."""
    filter_repo = PathFilterRepository(session)
    if not filter_repo.delete_for_library(filter_id=filter_id, library_id=library_id):
        raise HTTPException(status_code=404, detail="Filter not found")
