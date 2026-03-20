"""Maintenance mode endpoints. Require tenant admin auth."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session, require_tenant_admin
from src.repository.system_metadata import SystemMetadataRepository

router = APIRouter(prefix="/v1/tenant/maintenance", tags=["maintenance"])

MAINTENANCE_KEY = "maintenance_mode"


def get_maintenance_state(session: Session) -> dict:
    """Read and parse the maintenance_mode system_metadata value.

    Returns a dict with keys: active (bool), message (str|None), started_at (str|None).
    If the key is absent, active=False. If the value is malformed JSON, treats it as active
    to err on the side of caution.
    """
    repo = SystemMetadataRepository(session)
    raw = repo.get_value(MAINTENANCE_KEY)
    if raw is None:
        return {"active": False, "message": None, "started_at": None}
    try:
        state = json.loads(raw)
        return {
            "active": bool(state.get("active", True)),
            "message": state.get("message"),
            "started_at": state.get("started_at"),
        }
    except Exception:
        # Malformed value — treat as active to avoid accidentally unblocking workers.
        return {"active": True, "message": raw, "started_at": None}


class MaintenanceStatusResponse(BaseModel):
    active: bool
    message: str | None = None
    started_at: str | None = None


class MaintenanceStartRequest(BaseModel):
    message: str = ""


@router.get("/status", response_model=MaintenanceStatusResponse)
def get_maintenance_status(
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_tenant_admin)],
) -> MaintenanceStatusResponse:
    state = get_maintenance_state(session)
    return MaintenanceStatusResponse(**state)


@router.post("/start", response_model=MaintenanceStatusResponse)
def start_maintenance(
    body: MaintenanceStartRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_tenant_admin)],
) -> MaintenanceStatusResponse:
    repo = SystemMetadataRepository(session)
    now = datetime.now(tz=timezone.utc).isoformat()
    value = json.dumps({"active": True, "message": body.message, "started_at": now})
    repo.set_value(MAINTENANCE_KEY, value)
    return MaintenanceStatusResponse(active=True, message=body.message or None, started_at=now)


@router.post("/end", response_model=MaintenanceStatusResponse)
def end_maintenance(
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_tenant_admin)],
) -> MaintenanceStatusResponse:
    repo = SystemMetadataRepository(session)
    repo.delete_key(MAINTENANCE_KEY)
    return MaintenanceStatusResponse(active=False)
