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
from src.server.repository.libraries import PathFilterRepository

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
    # Days a key a person made may go unused before upkeep ends it; None (the
    # default) when keys end only when someone revokes them.
    key_idle_days: int | None = None


class TenantSettingsUpdate(BaseModel):
    """Fields left out stay as they are; null means the whole video, or (trash_days) never."""

    video_preview_max_seconds: _Seconds | None = None
    public_video_preview_max_seconds: _Seconds | None = None
    follow_moves: StrictBool = True
    trash_days: _Days | None = None
    # Fewer trash days (or turning them back on) deletes for good, on the next
    # upkeep, what's already been in the trash longer: say yes to that.
    confirm_purge: StrictBool = False
    key_idle_days: _Days | None = None
    # Fewer key-idle days (or turning them on) ends, on the next upkeep, the
    # keys already unused for longer: say yes to that.
    confirm_end_keys: StrictBool = False


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
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> TenantSettingsResponse:
    """Account-wide settings. Anyone signed in can read them."""
    return _settings(session, request.state.tenant_id)


def _key_idle_days(tenant_id: str) -> int | None:
    from src.server.database import get_control_session
    from src.server.repository.control_plane import TenantRepository

    with get_control_session() as ctrl:
        return TenantRepository(ctrl).key_idle_days(tenant_id)


def _settings(session: Session, tenant_id: str) -> TenantSettingsResponse:
    return TenantSettingsResponse(
        video_preview_max_seconds=get_video_preview_max_seconds(session),
        public_video_preview_max_seconds=get_public_video_preview_max_seconds(session),
        follow_moves=get_follow_moves(session),
        trash_days=get_trash_days(session),
        key_idle_days=_key_idle_days(tenant_id),
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

    before = _settings(session, request.state.tenant_id)
    # Asked before anything is saved: a 409 changes nothing.
    if "trash_days" in body.model_fields_set:
        _ask_before_shortening(session, before.trash_days, body.trash_days, body.confirm_purge)
    if "key_idle_days" in body.model_fields_set:
        _ask_before_ending_keys(request.state.tenant_id, before.key_idle_days, body.key_idle_days,
                                body.confirm_end_keys)
    if "video_preview_max_seconds" in body.model_fields_set:
        set_video_preview_max_seconds(session, body.video_preview_max_seconds)
    if "public_video_preview_max_seconds" in body.model_fields_set:
        set_public_video_preview_max_seconds(session, body.public_video_preview_max_seconds)
    if "follow_moves" in body.model_fields_set:
        set_follow_moves(session, body.follow_moves)
    if "trash_days" in body.model_fields_set:
        set_trash_days(session, body.trash_days)
    if "key_idle_days" in body.model_fields_set:
        from src.server.database import get_control_session
        from src.server.repository.control_plane import TenantRepository

        with get_control_session() as ctrl:
            TenantRepository(ctrl).set_key_idle_days(request.state.tenant_id, body.key_idle_days)
    after = _settings(session, request.state.tenant_id)
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
    from src.server.repository.assets import AssetRepository
    from src.shared.utils import utcnow

    counts = AssetRepository(session).count_expiring(utcnow() - timedelta(days=new))
    if any(counts.values()):
        one = {"clips": "clip", "libraries": "library", "projects": "project"}
        what = ", ".join(f"{n} {one[k] if n == 1 else k}" for k, n in counts.items() if n)
        raise DecisionRequiredError(
            "trash_days_shortened",
            f"With {new} trash days, {what} in the trash for longer would be deleted for good "
            f"{'once upkeep is resumed' if lineage.upkeep_paused(session) else 'within minutes'}. "
            "Send confirm_purge: true to go ahead.",
            {"trash_days": new, **counts},
        )


def _ask_before_ending_keys(tenant_id: str, old: int | None, new: int | None, confirmed: bool) -> None:
    """409 keys_would_end when the new key-idle days would end keys on the
    next upkeep that the old ones kept (fewer days, or turned on), naming
    them (details.keys: key_id, label, role, last_used_at)."""
    if new is None or (old is not None and new >= old) or confirmed:
        return
    from datetime import timedelta

    from src.server.database import get_control_session
    from src.server.repository.control_plane import ApiKeyRepository
    from src.shared.utils import utcnow

    with get_control_session() as ctrl:
        ending = ApiKeyRepository(ctrl).unused_since(tenant_id, utcnow() - timedelta(days=new))
        keys = [{"key_id": k.key_id, "label": k.label, "role": k.role,
                 "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None} for k in ending]
    if keys:
        raise DecisionRequiredError(
            "keys_would_end",
            f"With {new} days, {len(keys)} {'key' if len(keys) == 1 else 'keys'} unused for longer would stop "
            "working within minutes. Send confirm_end_keys: true to go ahead.",
            {"key_idle_days": new, "keys": keys},
        )


@router.get("/context", response_model=TenantContextResponse)
def get_tenant_context(request: Request) -> TenantContextResponse:
    """
    Return tenant_id and vision API config for the authenticated tenant.
    Used by CLI for ingest pipeline configuration.
    """
    from src.server.database import get_control_session
    from src.server.repository.control_plane import TenantRepository

    from src.server.repository.ai_machines import first_vision_machine, job_model, key_for

    tenant_id = request.state.tenant_id
    with get_control_session() as session:
        tenant = TenantRepository(session).get_by_id(tenant_id)
        # Clients that know one endpoint (the Mac app) get the first machine doing vision (/v1/ai).
        machine = first_vision_machine(session, tenant_id)

    return TenantContextResponse(
        tenant_id=tenant_id,
        vision_api_url=machine.api_url if machine else "",
        # Never the key (write-only: the scheduler's own key reads it from /v1/ai/jobs); the Mac app keeps its own.
        vision_api_key=key_for(machine, getattr(request.state, "scheduler", False)),
        vision_model_id=job_model(tenant, "vision"),
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
    _: Annotated[None, Depends(require_tenant_admin)],
) -> TenantFilterDefaultItemWithType:
    """Add a path filter default for the tenant. Returns 400 if pattern invalid.
    An account admin's: the defaults reach every library's reader, so what
    every library's scans keep."""
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
    _: Annotated[None, Depends(require_tenant_admin)],
) -> None:
    """Remove a path filter default. Returns 404 if not found. An account admin's, as adding one is."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Tenant context missing")
    filter_repo = PathFilterRepository(session)
    if not filter_repo.delete_default(default_id=default_id, tenant_id=tenant_id):
        raise HTTPException(status_code=404, detail="Filter default not found")


# -- Setting up (the first-run wizard) -----------------------------------------------

# The wizard's steps after the first admin (the web app's /welcome), in order:
# where previews live, who can open it, what to find, AI machines, people,
# the first library. Each is a Settings section; the wizard only remembers
# which ones the admin went through, so it can be left and picked up again.
SETUP_STEPS = ("storage", "network", "find", "ai", "people", "library")
# The install's, not the account's: a server admin's steps only (ADR-018 decision 8).
SERVER_STEPS = ("storage", "network")
SETUP_KEY = "setup.wizard"


class SetupProgress(BaseModel):
    steps: list[str]  # the caller's: the install's own two only for a server admin
    done: list[str]  # gone through (changed, or kept as they were): in step order
    dismissed: bool  # an admin said they'd rather not: the Admin page stops offering it
    finished: bool  # every one of the caller's steps done


class SetupChange(BaseModel):
    """Fields left out stay as they are."""

    done: list[str] | None = Field(default=None, max_length=len(SETUP_STEPS))
    dismissed: StrictBool | None = None


def _setup(session: Session, request: Request) -> SetupProgress:
    import json

    from src.server.repository.system_metadata import SystemMetadataRepository

    raw = SystemMetadataRepository(session).get_value(SETUP_KEY)
    try:
        kept = json.loads(raw) if raw else {}
    except ValueError:
        kept = {}
    done = [s for s in SETUP_STEPS if s in set(kept.get("done") or [])]
    server_admin = getattr(request.state, "server_admin", False) is True
    steps = [s for s in SETUP_STEPS if server_admin or s not in SERVER_STEPS]
    return SetupProgress(steps=steps, done=done, dismissed=bool(kept.get("dismissed")),
                         finished=all(s in done for s in steps))


@router.get("/setup", response_model=SetupProgress, dependencies=[Depends(require_tenant_admin)])
def get_setup(request: Request, session: Annotated[Session, Depends(get_tenant_session)]) -> SetupProgress:
    """How far the account's admins got through setting it up (the wizard)."""
    return _setup(session, request)


@router.put("/setup", response_model=SetupProgress, dependencies=[Depends(require_tenant_admin)])
def change_setup(body: SetupChange, request: Request,
                 session: Annotated[Session, Depends(get_tenant_session)]) -> SetupProgress:
    """Steps gone through (the whole list, as the wizard has it), and whether it's put away."""
    import json

    from src.server.api.errors import InvalidChoiceError
    from src.server.repository.system_metadata import SystemMetadataRepository

    now = _setup(session, request)
    done = now.done if body.done is None else body.done
    unknown = sorted(set(done) - set(SETUP_STEPS))
    if unknown:
        raise InvalidChoiceError("unknown_step", f"There's no step {unknown[0]!r}.", {"steps": list(SETUP_STEPS)})
    value = {"done": [s for s in SETUP_STEPS if s in set(done)],
             "dismissed": now.dismissed if body.dismissed is None else body.dismissed}
    SystemMetadataRepository(session).set_value(SETUP_KEY, json.dumps(value))  # commits
    return _setup(session, request)
