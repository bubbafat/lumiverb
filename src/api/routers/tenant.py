"""Tenant context and filter-defaults endpoints."""

# TODO: replace with GET /v1/tenant/id that returns only tenant_id.
# connection_string should never be exposed to clients.
# Blocked on: worker CLI refactor to not need connection_string.

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session, require_editor
from src.core.path_filter import validate_pattern
from src.repository.tenant import PathFilterRepository

router = APIRouter(prefix="/v1/tenant", tags=["tenant"])


class TenantContextResponse(BaseModel):
    tenant_id: str
    # connection_string removed — workers must not have direct DB access


class TenantFilterDefaultItem(BaseModel):
    default_id: str
    pattern: str
    created_at: str


class TenantFilterDefaultItemWithType(BaseModel):
    default_id: str
    type: str
    pattern: str
    created_at: str


class TenantFilterDefaultsResponse(BaseModel):
    includes: list[TenantFilterDefaultItem]
    excludes: list[TenantFilterDefaultItem]


class CreateTenantFilterDefaultRequest(BaseModel):
    type: str  # "include" | "exclude"
    pattern: str


@router.get("/context", response_model=TenantContextResponse)
def get_tenant_context(request: Request) -> TenantContextResponse:
    """
    Return tenant_id for the authenticated tenant.
    Used by CLI/worker for storage path computation only.
    """
    return TenantContextResponse(tenant_id=request.state.tenant_id)


@router.get("/filter-defaults", response_model=TenantFilterDefaultsResponse)
def list_filter_defaults(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> TenantFilterDefaultsResponse:
    """Return include and exclude path filter defaults for the current tenant."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Tenant context missing")
    filter_repo = PathFilterRepository(session)
    raw = filter_repo.list_defaults(tenant_id)
    includes = [
        TenantFilterDefaultItem(default_id=d.default_id, pattern=d.pattern, created_at=d.created_at.isoformat())
        for d in raw if d.type == "include"
    ]
    excludes = [
        TenantFilterDefaultItem(default_id=d.default_id, pattern=d.pattern, created_at=d.created_at.isoformat())
        for d in raw if d.type == "exclude"
    ]
    return TenantFilterDefaultsResponse(includes=includes, excludes=excludes)


@router.post("/filter-defaults", response_model=TenantFilterDefaultItemWithType, status_code=201)
def create_filter_default(
    request: Request,
    body: CreateTenantFilterDefaultRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> TenantFilterDefaultItemWithType:
    """Add a path filter default for the tenant. Returns 400 if pattern invalid."""
    if body.type not in ("include", "exclude"):
        raise HTTPException(status_code=400, detail="type must be 'include' or 'exclude'")
    try:
        validate_pattern(body.pattern)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Tenant context missing")
    filter_repo = PathFilterRepository(session)
    row = filter_repo.add_default(tenant_id=tenant_id, type=body.type, pattern=body.pattern)
    return TenantFilterDefaultItemWithType(
        default_id=row.default_id,
        type=row.type,
        pattern=row.pattern,
        created_at=row.created_at.isoformat(),
    )


@router.delete("/filter-defaults/{default_id}", status_code=204)
def delete_filter_default(
    default_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> None:
    """Remove a path filter default. Returns 404 if not found."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Tenant context missing")
    filter_repo = PathFilterRepository(session)
    if not filter_repo.delete_default(default_id=default_id, tenant_id=tenant_id):
        raise HTTPException(status_code=404, detail="Filter default not found")
