"""Tenant context and filter-defaults endpoints."""

# TODO: replace with GET /v1/tenant/id that returns only tenant_id.
# connection_string should never be exposed to clients.
# Blocked on: worker CLI refactor to not need connection_string.

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import Session

from src.server.api.dependencies import get_tenant_session, require_editor, require_tenant_admin
from src.server.tenant_settings import (
    get_public_video_preview_max_seconds,
    get_video_preview_max_seconds,
    set_public_video_preview_max_seconds,
    set_video_preview_max_seconds,
)
from src.shared.path_filter import validate_pattern
from src.server.repository.tenant import PathFilterRepository

router = APIRouter(prefix="/v1/tenant", tags=["tenant"])


class TenantContextResponse(BaseModel):
    tenant_id: str
    vision_api_url: str = ""
    vision_api_key: str = ""
    vision_model_id: str = ""


_Seconds = Annotated[int, Field(strict=True, ge=1, le=86_400)]


class TenantSettingsResponse(BaseModel):
    # Seconds of each video playback serves signed in; None means the whole video.
    video_preview_max_seconds: int | None = None
    # The same on public pages (never more than the above); 10 until set.
    public_video_preview_max_seconds: int | None = 10


class TenantSettingsUpdate(BaseModel):
    """Fields left out stay as they are; null means the whole video."""

    video_preview_max_seconds: _Seconds | None = None
    public_video_preview_max_seconds: _Seconds | None = None


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


@router.get("/settings", response_model=TenantSettingsResponse)
def get_tenant_settings(
    session: Annotated[Session, Depends(get_tenant_session)],
) -> TenantSettingsResponse:
    """Account-wide settings. Anyone signed in can read them."""
    return _settings(session)


def _settings(session: Session) -> TenantSettingsResponse:
    return TenantSettingsResponse(
        video_preview_max_seconds=get_video_preview_max_seconds(session),
        public_video_preview_max_seconds=get_public_video_preview_max_seconds(session),
    )


@router.patch(
    "/settings",
    response_model=TenantSettingsResponse,
    dependencies=[Depends(require_tenant_admin)],
)
def update_tenant_settings(
    body: TenantSettingsUpdate,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> TenantSettingsResponse:
    """Change account-wide settings (admins only). A new cap drops the cuts made for the old one."""
    from src.server.api.routers.playback import clear_cuts

    before = _settings(session)
    if "video_preview_max_seconds" in body.model_fields_set:
        set_video_preview_max_seconds(session, body.video_preview_max_seconds)
    if "public_video_preview_max_seconds" in body.model_fields_set:
        set_public_video_preview_max_seconds(session, body.public_video_preview_max_seconds)
    after = _settings(session)
    if after != before:
        clear_cuts(request.state.tenant_id)
    return after


@router.get("/context", response_model=TenantContextResponse)
def get_tenant_context(request: Request) -> TenantContextResponse:
    """
    Return tenant_id and vision API config for the authenticated tenant.
    Used by CLI for ingest pipeline configuration.
    """
    from src.server.database import get_control_session
    from src.server.repository.control_plane import TenantRepository

    tenant_id = request.state.tenant_id
    with get_control_session() as session:
        tenant = TenantRepository(session).get_by_id(tenant_id)

    return TenantContextResponse(
        tenant_id=tenant_id,
        vision_api_url=tenant.vision_api_url if tenant else "",
        vision_api_key=tenant.vision_api_key if tenant else "",
        vision_model_id=tenant.vision_model_id if tenant else "",
    )


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
