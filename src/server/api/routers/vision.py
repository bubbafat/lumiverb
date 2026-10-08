"""The account's vision AI: the endpoint, its key, and the model (ADR-016 phase 3).

The one place they live (workers keep none of their own). An admin enters
the URL and an optional key in Settings → AI and presses Connect, which
lists the endpoint's models; they pick one and save, and the endpoint is
asked again so what's saved is a model it offers. The worker checks the
model is still offered before vision work; when it isn't, the worker stops
that work and reports why here, and Settings shows it until it's fixed.
Changing the model makes descriptions, OCR and scene descriptions stale.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, StrictBool
from sqlmodel import Session

from src.server.api.dependencies import get_tenant_session, require_editor, require_signed_in, require_tenant_admin
from src.server.api.errors import ConflictError, UpstreamError
from src.server.tenant_settings import get_vision_status, set_vision_status
from src.shared.utils import utcnow
from src.shared.vision_endpoint import VisionEndpointError, list_models

router = APIRouter(prefix="/v1/tenant/vision", tags=["vision"])


class VisionStatus(BaseModel):
    ok: bool
    error: str = ""
    model: str = ""
    api_url: str = ""
    checked_at: datetime | None = None


class VisionSettings(BaseModel):
    api_url: str
    has_key: bool
    model: str
    # The worker's last check, for these settings; None until it has checked.
    status: VisionStatus | None = None


class VisionConnect(BaseModel):
    api_url: str = Field(min_length=1, max_length=500)
    # None: the key saved now (for the same URL); "": none.
    api_key: str | None = Field(default=None, max_length=500)


class VisionModels(BaseModel):
    models: list[str]


class VisionSave(BaseModel):
    # "" turns vision AI off.
    api_url: str = Field(default="", max_length=500)
    # None: keep the saved key; "": none.
    api_key: str | None = Field(default=None, max_length=500)
    model: str = Field(default="", max_length=200)


class VisionStatusIn(BaseModel):
    ok: StrictBool
    error: str = Field(default="", max_length=2000)
    model: str = Field(default="", max_length=200)
    api_url: str = Field(default="", max_length=500)


def _tenant(request: Request):
    from src.server.database import get_control_session
    from src.server.repository.control_plane import TenantRepository

    with get_control_session() as ctrl:
        return TenantRepository(ctrl).get_by_id(request.state.tenant_id)


def _key_for(request: Request, api_url: str, api_key: str | None) -> str | None:
    """The key to use: the one sent, else the saved one when the URL is the saved URL."""
    if api_key is not None:
        return api_key or None
    tenant = _tenant(request)
    if tenant and tenant.vision_api_key and tenant.vision_api_url.rstrip("/") == api_url.strip().rstrip("/"):
        return tenant.vision_api_key
    return None


def _models(api_url: str, api_key: str | None) -> list[str]:
    try:
        return list_models(api_url, api_key)
    except VisionEndpointError as e:
        raise UpstreamError("vision_unreachable", str(e), {"api_url": api_url}) from None


def _settings(request: Request, session: Session) -> VisionSettings:
    tenant = _tenant(request)
    api_url = (tenant.vision_api_url if tenant else "") or ""
    model = (tenant.vision_model_id if tenant else "") or ""
    status = get_vision_status(session)
    # A check of other settings than these says nothing about them.
    if status and (status.get("model") != model or status.get("api_url") != api_url):
        status = None
    return VisionSettings(api_url=api_url, has_key=bool(tenant and tenant.vision_api_key), model=model,
                          status=VisionStatus(**status) if status else None)


@router.get("", response_model=VisionSettings, dependencies=[Depends(require_signed_in)])
def get_vision(request: Request, session: Annotated[Session, Depends(get_tenant_session)]) -> VisionSettings:
    """The endpoint, whether a key is saved (never the key), the model, and
    the worker's last check of them."""
    return _settings(request, session)


@router.post("/connect", response_model=VisionModels, dependencies=[Depends(require_tenant_admin)])
def connect(body: VisionConnect, request: Request) -> VisionModels:
    """Ask the endpoint which models it offers. 502 vision_unreachable with
    why when it can't say (unreachable, refused the key, not OpenAI-compatible,
    no models)."""
    return VisionModels(models=_models(body.api_url, _key_for(request, body.api_url, body.api_key)))


@router.put("", response_model=VisionSettings, dependencies=[Depends(require_tenant_admin)])
def save(body: VisionSave, request: Request, session: Annotated[Session, Depends(get_tenant_session)]) -> VisionSettings:
    """Save the endpoint, key and model. The endpoint is asked again: 409
    vision_model_unavailable (details.models) when it doesn't offer the
    model, 502 vision_unreachable when it can't say. An empty URL turns
    vision AI off (and forgets the key and model)."""
    from src.server.database import get_control_session
    from src.server.repository.control_plane import TenantRepository

    api_url = body.api_url.strip().rstrip("/")
    key = _key_for(request, api_url, body.api_key) if api_url else None
    model = body.model.strip() if api_url else ""
    if api_url:
        if not model:
            raise ConflictError("vision_model_unavailable", "Pick a model.", {"models": _models(api_url, key)})
        models = _models(api_url, key)
        if model not in models:
            raise ConflictError("vision_model_unavailable", f"{api_url} doesn't offer {model}.", {"models": models})
    with get_control_session() as ctrl:
        tenant = TenantRepository(ctrl).get_by_id(request.state.tenant_id)
        tenant.vision_api_url = api_url
        tenant.vision_api_key = key or ""
        tenant.vision_model_id = model
        ctrl.add(tenant)
        ctrl.commit()
    # Connect just showed these work; the worker checks them again before using them.
    set_vision_status(session, {"ok": True, "error": "", "model": model, "api_url": api_url,
                                "checked_at": utcnow().isoformat()} if api_url else None)
    return _settings(request, session)


@router.post("/status", response_model=VisionSettings, dependencies=[Depends(require_editor)])
def report_status(body: VisionStatusIn, request: Request,
                  session: Annotated[Session, Depends(get_tenant_session)]) -> VisionSettings:
    """The worker's check of the endpoint before vision work: ok, or why it
    stopped (the model isn't offered, the endpoint doesn't answer). Not a
    failure of any clip."""
    set_vision_status(session, {"ok": body.ok, "error": "" if body.ok else body.error, "model": body.model,
                                "api_url": body.api_url.rstrip("/"), "checked_at": utcnow().isoformat()})
    return _settings(request, session)
