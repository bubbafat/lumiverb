"""Plug-ins (ADR-021): optional parts of Lumiverb that an account turns on
or off, and what turning one on asks first.

GET /v1/plugins                — every plug-in, its state in this account, and what it asks (signed in)
PUT /v1/plugins/{plugin_id}    — turn one on or off (account admins)

Turning on a plug-in that declares an acknowledgment (a license, say) is a
409 acknowledgment_required carrying its text and version until the request
says it accepted that version: no client turns one on without showing it.
Each acceptance is kept for good (who, which version, when).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import Session

from src.plugins import plugin as plugin_spec
from src.plugins import registry
from src.plugins.contract import Acknowledgment, PluginSpec
from src.server.api.dependencies import get_tenant_session, require_signed_in, require_tenant_admin
from src.server.api.errors import DecisionRequiredError
from src.server.database import get_control_session
from src.server.repository.control_plane import UserRepository
from src.server.repository.plugins import ON, PluginRepository, state_of
from src.shared.utils import utcnow

router = APIRouter(prefix="/v1/plugins", tags=["plugins"])


class LinkOut(BaseModel):
    label: str
    url: str


class AcknowledgmentOut(BaseModel):
    version: str
    title: str
    text: str  # paragraphs, separated by a blank line
    links: list[LinkOut]


class Accepted(BaseModel):
    version: str
    # Who accepted it: their email, for an account admin; None otherwise, or for an API key.
    by: str | None = None
    at: datetime


class PluginOut(BaseModel):
    plugin_id: str
    title: str
    description: str
    # on; off; waiting: turned on, but its acknowledgment's text changed since it was accepted (off until accepted)
    state: Literal["on", "off", "waiting"]
    acknowledgment: AcknowledgmentOut | None = None
    # The acceptance it's on by (or waiting with), while it's on or waiting.
    acknowledged: Accepted | None = None


class PluginList(BaseModel):
    plugins: list[PluginOut]


class PluginChoice(BaseModel):
    enabled: bool
    # The version of its acknowledgment accepted, turning on a plug-in that asks one.
    acknowledged: str | None = Field(default=None, max_length=64)


def _acknowledgment(ack: Acknowledgment | None) -> AcknowledgmentOut | None:
    if ack is None:
        return None
    return AcknowledgmentOut(version=ack.version, title=ack.title, text=ack.text,
                             links=[LinkOut(label=link.label, url=link.url) for link in ack.links])


def _email(user_id: str | None) -> str | None:
    if not user_id or user_id.startswith("key:"):
        return None
    with get_control_session() as control:
        user = UserRepository(control).get_by_id(user_id)
        return user.email if user else None


def _out(spec: PluginSpec, row: dict | None, accepted: dict | None, *, admin: bool) -> PluginOut:
    state = state_of(spec, row)
    shown = None
    if spec.acknowledgment is not None and state != "off" and accepted is not None:
        shown = Accepted(version=accepted["version"], by=_email(accepted["user_id"]) if admin else None,
                         at=accepted["acknowledged_at"])
    return PluginOut(plugin_id=spec.plugin_id, title=spec.title, description=spec.description, state=state,
                     acknowledgment=_acknowledgment(spec.acknowledgment), acknowledged=shown)


def _admin(request: Request) -> bool:
    return getattr(request.state, "role", None) == "admin"


@router.get("", response_model=PluginList, dependencies=[Depends(require_signed_in)])
def list_plugins(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> PluginList:
    """Every plug-in, its state in this account and what turning it on asks."""
    repo = PluginRepository(session)
    rows, accepted = repo.rows(), repo.accepted()
    return PluginList(plugins=[_out(p, rows.get(p.plugin_id), accepted.get(p.plugin_id), admin=_admin(request))
                               for p in registry().values()])


@router.put("/{plugin_id}", response_model=PluginOut, dependencies=[Depends(require_tenant_admin)])
def set_plugin(
    plugin_id: str,
    body: PluginChoice,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> PluginOut:
    """Turn a plug-in on or off in this account. Turning on one that asks
    an acknowledgment: 409 acknowledgment_required with its text and version
    (details) unless acknowledged is that version; accepting is kept for
    good. On already, nothing changes. Off asks nothing."""
    spec = plugin_spec(plugin_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="No such plug-in")
    repo = PluginRepository(session)
    by = getattr(request.state, "user_id", None) or (
        f"key:{request.state.key_id}" if getattr(request.state, "key_id", None) else None)
    now = utcnow()
    row = repo.rows().get(plugin_id)
    if not body.enabled:
        repo.set(plugin_id, False, acknowledged_version=None, by=by, now=now)
    elif state_of(spec, row) != ON:
        ack = spec.acknowledgment
        if ack is not None and body.acknowledged != ack.version:
            raise DecisionRequiredError(
                "acknowledgment_required",
                f"Turning on {spec.title} asks you to read and accept {ack.title} first; then send "
                f"acknowledged: \"{ack.version}\".",
                {"plugin_id": plugin_id, **_acknowledgment(ack).model_dump()},
            )
        if ack is not None:
            repo.acknowledge(plugin_id, ack.version, user_id=by, now=now)
        repo.set(plugin_id, True, acknowledged_version=ack.version if ack else None, by=by, now=now)
    session.commit()
    return _out(spec, repo.rows().get(plugin_id), repo.accepted().get(plugin_id), admin=True)
