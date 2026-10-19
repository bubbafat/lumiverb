"""Plug-ins (ADR-021, ADR-023): optional parts of Lumiverb that an account
turns on or off, and what turning one on asks first. Lumiverb's own (its
code's) and those the server admin installed (programs of their own, each
with its manifest: src/server/plugin_catalog.py), an installed one as its
default says in an account whose admin hasn't chosen.

GET /v1/plugins                — every plug-in, its state in this account, and what it asks (signed in)
PUT /v1/plugins/{plugin_id}    — turn one on or off (account admins)

What a plug-in keeps for the whole server (its spec's data: GeoNames'
place list) is fetched in the background when an account turns it on
(src/server/plugin_data.py); its state is each plug-in's `data`.

Turning on a plug-in that declares an acknowledgment (a license, say) is a
409 acknowledgment_required carrying its text and version until the request
says it accepted that version: no client turns one on without showing it.
Each acceptance is kept for good (who, which version, when).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import Session

from src.plugins.contract import Acknowledgment
from src.plugins.manifest import INPUTS, LOOKUPS
from src.server import plugin_data
from src.server.api.dependencies import get_tenant_session, require_signed_in, require_tenant_admin
from src.server.api.errors import ConflictError, DecisionRequiredError
from src.server.config import get_settings
from src.server.database import get_control_session
from src.server.plugin_catalog import Entry, entries, find
from src.server.plugin_runner import STATES, read_state
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


class DataOut(BaseModel):
    """What it keeps for the whole server, as it's fetched."""

    title: str  # what it is: "The place list"
    unit: str  # what count counts: "places"
    # missing (never fetched), fetching, ready, failed
    state: Literal["missing", "fetching", "ready", "failed"]
    done: int = 0  # bytes downloaded so far, while fetching
    total: int | None = None  # bytes in all, when known
    detail: str | None = None  # what it's doing now
    error: str | None = None  # why it failed
    count: int | None = None  # how many it keeps (places), once ready
    ready_at: datetime | None = None


class RunningOut(BaseModel):
    """What the plug-in runner last said of an installed one (ADR-023): starting, ready, preparing (done of
    total, detail), failing (it says it can't work: why), restarting (it stopped: why; started again soon),
    stopped (left stopped: why), or not_running (the runner itself isn't, or hasn't started it yet)."""

    state: Literal["starting", "ready", "preparing", "failing", "restarting", "stopped", "not_running"]
    why: str | None = None
    detail: str | None = None
    done: int | None = None
    total: int | None = None
    since: datetime | None = None
    memory_mb: int | None = None  # what it uses, as last seen
    restarts: int = 0


class DownloadOut(BaseModel):
    url: str
    title: str
    about_mb: int


class PluginOut(BaseModel):
    plugin_id: str
    title: str
    description: str
    # on; off; waiting: turned on, but its acknowledgment must be accepted again since (off until accepted)
    state: Literal["on", "off", "waiting"]
    acknowledgment: AcknowledgmentOut | None = None
    # The acceptance it's on by (or waiting with), while it's on or waiting.
    acknowledged: Accepted | None = None
    # What it keeps for the whole server (None: nothing).
    data: DataOut | None = None
    # built_in: Lumiverb's own; installed: a program of its own the server admin installed (ADR-023).
    kind: Literal["built_in", "installed"] = "built_in"
    # An installed one's: its version, who made it, and where to read about it.
    version: str | None = None
    author: str | None = None
    homepage: str | None = None
    # An installed one's default, for accounts whose admin hasn't turned it on or off; and whether this one hasn't.
    on_by_default: bool | None = None
    follows_default: bool = False
    # An installed one's, in words: what it reads, what it answers.
    reads: list[str] = Field(default_factory=list)
    answers: list[str] = Field(default_factory=list)
    downloads: list[DownloadOut] = Field(default_factory=list)
    internet: bool = False
    # An installed one's process, as the runner last said (None for Lumiverb's own).
    running: RunningOut | None = None


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


NOT_RUNNING = "The plug-in runner isn't running"


def _running(plugin_id: str, said: dict | None) -> RunningOut:
    mine = (said or {}).get(plugin_id)
    if not isinstance(mine, dict) or mine.get("state") not in STATES:
        return RunningOut(state="not_running", why=NOT_RUNNING if said is None else "Not started yet")
    try:
        return RunningOut(**{k: mine.get(k) for k in RunningOut.model_fields if mine.get(k) is not None})
    except ValueError:
        return RunningOut(state="not_running", why="What the runner said isn't readable")


def _said() -> dict | None:
    return read_state(Path(get_settings().data_dir))


def _out(entry: Entry, row: dict | None, accepted: dict | None, *, admin: bool,
         said: dict | None = None) -> PluginOut:
    spec, installed = entry.spec, entry.installed
    state = state_of(spec, row, installed.default if installed else None)
    shown = None
    if row is None and installed is not None and state != "off" and installed.acknowledged_version:
        # On by default: the server admin accepted it, for every account that hasn't chosen.
        shown = Accepted(version=installed.acknowledged_version, at=installed.changed_at)
    elif spec.acknowledgment is not None and state != "off" and accepted is not None:
        shown = Accepted(version=accepted["version"], by=_email(accepted["user_id"]) if admin else None,
                         at=accepted["acknowledged_at"])
    data = plugin_data.status(spec.plugin_id) if installed is None else None
    out = PluginOut(plugin_id=spec.plugin_id, title=spec.title, description=spec.description, state=state,
                    acknowledgment=_acknowledgment(spec.acknowledgment), acknowledged=shown,
                    data=DataOut(title=spec.data_title, unit=spec.data_unit,
                                 **{k: data.get(k) for k in DataOut.model_fields
                                    if k not in ("title", "unit") and data.get(k) is not None})
                    if data else None)
    if installed is not None:
        m = installed.manifest
        out = out.model_copy(update={
            "kind": "installed", "version": m.version, "author": m.author or None, "homepage": m.homepage or None,
            "on_by_default": installed.on_by_default, "follows_default": row is None,
            "reads": [INPUTS[i] for i in (m.work.inputs if m.work else ())],
            "answers": [LOOKUPS[name] for name in m.lookups],
            "downloads": [DownloadOut(url=d.url, title=d.title, about_mb=d.about_mb) for d in m.downloads],
            "internet": m.internet, "running": _running(spec.plugin_id, said)})
    return out


def require_on(session: Session, plugin_id: str) -> None:
    """409 plugin_off unless the plug-in is on in this account (details: plugin_id, title)."""
    if not PluginRepository(session).is_on(plugin_id):
        entry = find(plugin_id)
        title = entry.spec.title if entry else plugin_id
        raise ConflictError("plugin_off", f"{title} is off: an admin turns it on in Settings › Plug-ins.",
                            {"plugin_id": plugin_id, "title": title})


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
    said = _said()
    return PluginList(plugins=[_out(e, rows.get(e.spec.plugin_id), accepted.get(e.spec.plugin_id),
                                    admin=_admin(request), said=said) for e in entries()])


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
    good. On already, nothing changes. Off asks nothing. On (newly or
    already), what it keeps for the server is fetched if it isn't there, a
    fetch that failed at once (Try again)."""
    entry = find(plugin_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="No such plug-in")
    spec, default = entry.spec, entry.installed.default if entry.installed else None
    repo = PluginRepository(session)
    by = getattr(request.state, "user_id", None) or (
        f"key:{request.state.key_id}" if getattr(request.state, "key_id", None) else None)
    now = utcnow()
    row = repo.rows().get(plugin_id)
    if not body.enabled:
        repo.set(plugin_id, False, acknowledged_version=None, by=by, now=now)
    elif state_of(spec, row, default) != ON:
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
    if body.enabled and entry.installed is None:
        plugin_data.ensure(plugin_id, retry_now=True)
    return _out(entry, repo.rows().get(plugin_id), repo.accepted().get(plugin_id), admin=True, said=_said())
