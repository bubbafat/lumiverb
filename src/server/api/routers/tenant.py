"""Tenant context and filter-defaults endpoints."""

# TODO: replace with GET /v1/tenant/id that returns only tenant_id.
# connection_string should never be exposed to clients.
# Blocked on: worker CLI refactor to not need connection_string.

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, StrictBool
from sqlmodel import Session

from src.server.api.dependencies import get_tenant_session, require_editor, require_tenant_admin
from src.server.api.errors import DecisionRequiredError
from src.server.tenant_settings import (
    get_follow_moves,
    get_public_video_preview_max_seconds,
    get_trash_days,
    get_video_preview_max_seconds,
    set_follow_moves,
    set_public_video_preview_max_seconds,
    set_trash_days,
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
_Days = Annotated[int, Field(strict=True, ge=1, le=3650)]


class TenantSettingsResponse(BaseModel):
    # Seconds of each video playback serves signed in; None means the whole video.
    video_preview_max_seconds: int | None = None
    # The same on public pages (never more than the above); 10 until set.
    public_video_preview_max_seconds: int | None = 10
    # The same content is the same asset: moves, renames, copy then delete.
    # Off, the path is the only identity.
    follow_moves: bool = True
    # Days clips, libraries and projects stay in the trash before they're
    # deleted for good; None when that's off (the trash is emptied by hand).
    trash_days: int | None = 30


class TenantSettingsUpdate(BaseModel):
    """Fields left out stay as they are; null means the whole video, or (trash_days) never."""

    video_preview_max_seconds: _Seconds | None = None
    public_video_preview_max_seconds: _Seconds | None = None
    follow_moves: StrictBool = True
    trash_days: _Days | None = None
    # Fewer trash days (or turning them back on) deletes for good, on the next
    # upkeep, what's already been in the trash longer: say yes to that.
    confirm_purge: StrictBool = False


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
        follow_moves=get_follow_moves(session),
        trash_days=get_trash_days(session),
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
    # Asked before anything is saved: a 409 changes nothing.
    if "trash_days" in body.model_fields_set:
        _ask_before_shortening(session, before.trash_days, body.trash_days, body.confirm_purge)
    if "video_preview_max_seconds" in body.model_fields_set:
        set_video_preview_max_seconds(session, body.video_preview_max_seconds)
    if "public_video_preview_max_seconds" in body.model_fields_set:
        set_public_video_preview_max_seconds(session, body.public_video_preview_max_seconds)
    if "follow_moves" in body.model_fields_set:
        set_follow_moves(session, body.follow_moves)
    if "trash_days" in body.model_fields_set:
        set_trash_days(session, body.trash_days)
    after = _settings(session)
    caps = ("video_preview_max_seconds", "public_video_preview_max_seconds")
    if any(getattr(after, c) != getattr(before, c) for c in caps):
        clear_cuts(request.state.tenant_id)
    return after


def _ask_before_shortening(session: Session, old: int | None, new: int | None, confirmed: bool) -> None:
    """409 trash_days_shortened when the new trash days would delete things on
    the next upkeep that the old ones kept (fewer days, or turned back on)."""
    if new is None or (old is not None and new >= old) or confirmed:
        return
    from datetime import timedelta

    from src.server.repository import lineage
    from src.server.repository.tenant import AssetRepository
    from src.shared.utils import utcnow

    counts = AssetRepository(session).count_expiring(utcnow() - timedelta(days=new))
    if any(counts.values()):
        one = {"clips": "clip", "libraries": "library", "projects": "project"}
        what = ", ".join(f"{n} {one[k] if n == 1 else k}" for k, n in counts.items() if n)
        raise DecisionRequiredError(
            "trash_days_shortened",
            f"With {new} trash days, {what} in the trash for longer would be deleted for good "
            f"{'once processing is resumed' if lineage.all_paused(session) else 'within minutes'}. "
            "Send confirm_purge: true to go ahead.",
            {"trash_days": new, **counts},
        )


@router.get("/context", response_model=TenantContextResponse)
def get_tenant_context(request: Request) -> TenantContextResponse:
    """
    Return tenant_id and vision API config for the authenticated tenant.
    Used by CLI for ingest pipeline configuration.
    """
    from src.server.database import get_control_session
    from src.server.repository.control_plane import TenantRepository

    from src.server.repository.ai_machines import first_vision_machine

    tenant_id = request.state.tenant_id
    with get_control_session() as session:
        tenant = TenantRepository(session).get_by_id(tenant_id)
        # Clients that know one endpoint (the Mac app) get the first machine doing vision (/v1/ai).
        machine = first_vision_machine(session, tenant_id)

    # The key is for whoever does vision work (the worker, an editor's key); never a viewer's.
    may_see_key = getattr(request.state, "role", None) in ("admin", "editor")
    return TenantContextResponse(
        tenant_id=tenant_id,
        vision_api_url=machine.api_url if machine else "",
        vision_api_key=(machine.api_key if machine and may_see_key else "") or "",
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
