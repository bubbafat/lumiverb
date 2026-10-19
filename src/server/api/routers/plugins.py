"""Plug-ins (ADR-021, ADR-023): optional parts of Lumiverb that an account
turns on or off, and what turning one on asks first. Lumiverb's own (its
code's) and those the server admin installed (programs of their own, each
with its manifest: src/server/plugin_catalog.py), an installed one as its
default says in an account whose admin hasn't chosen.

GET /v1/plugins                              — every plug-in, its state in this account, and what it asks (signed in)
PUT /v1/plugins/{plugin_id}                  — turn one on or off (account admins)
PUT /v1/plugins/{plugin_id}/settings         — an installed one's settings for this account (account admins)
PUT /v1/plugins/{plugin_id}/server-settings  — an installed one's settings for the whole server (server admins)

An installed plug-in's settings are its manifest's (ADR-023 phase 1c):
each checked by its schema before it's saved, then by the plug-in itself
when its manifest says it checks them (POST /v1/settings/check on its
socket), each error said by key (422 invalid_settings). A secret's value is
never shown again; the server's settings only to server admins.

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
from src.server.api.dependencies import (
    get_current_user_id,
    get_tenant_session,
    require_server_admin,
    require_signed_in,
    require_tenant_admin,
)
from src.server.api.errors import ApiError, ConflictError, DecisionRequiredError
from src.server.config import get_settings
from src.server.database import get_control_session
from src.server.plugin_catalog import Entry, entries, find
from src.server.plugin_client import PluginAnswerError, ask, socket_path
from src.server.plugin_runner import READY, STATES, read_state
from src.server.plugin_settings import ACCOUNT, SERVER, PluginSettingsRepository, changed, values
from src.server.plugin_settings import shown as setting_shown
from src.server.repository import lineage
from src.server.repository.control_plane import UserRepository
from src.server.repository.plugins import ON, PluginRepository, state_of
from src.shared.producers import PRODUCERS
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


class ChoiceOut(BaseModel):
    value: str
    label: str


class SettingOut(BaseModel):
    """An installed plug-in's setting, its schema as its manifest declares it, and its value here."""

    key: str
    type: Literal["int", "float", "bool", "text", "choice", "secret"]
    label: str
    help: str
    default: int | float | bool | str | None = None  # None for a secret
    value: int | float | bool | str | None = None  # None for a secret: never shown again
    set: bool = False  # chosen here (a secret: set at all)
    minimum: float | None = None
    maximum: float | None = None
    unit: str | None = None
    pattern: str | None = None
    max_length: int | None = None
    choices: list[ChoiceOut] = Field(default_factory=list)
    scope: Literal["account", "server"]
    advanced: bool = False
    remakes: bool = False
    editable: bool = False  # whether the caller may change it


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
    # The lookups it answers, by name ("places": Set location's Place search).
    lookups: list[str] = Field(default_factory=list)
    downloads: list[DownloadOut] = Field(default_factory=list)
    internet: bool = False
    # An installed one's process, as the runner last said (None for Lumiverb's own).
    running: RunningOut | None = None
    # An installed one's settings: the account's, and the server's for a server admin.
    settings: list[SettingOut] = Field(default_factory=list)
    # Saving settings: why the plug-in's own check didn't run (it isn't running); they were saved.
    unchecked: str | None = None


class SettingsChange(BaseModel):
    # Each setting it changes: its value, or null for its default (a secret: cleared). Others stay as they are.
    settings: dict[str, int | float | bool | str | None] = Field(max_length=100)
    # A change of a setting that remakes (an account's): what becomes of what the old settings made, asked
    # with 409 redo_on_change until this says: true remakes them too, false is new ones only.
    redo: bool | None = None


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
         said: dict | None = None, server_admin: bool = False, chosen: dict | None = None,
         server_chosen: dict | None = None) -> PluginOut:
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
            "answers": [LOOKUPS[name] for name in m.lookups], "lookups": list(m.lookups),
            "downloads": [DownloadOut(url=d.url, title=d.title, about_mb=d.about_mb) for d in m.downloads],
            "internet": m.internet, "running": _running(spec.plugin_id, said),
            "settings": [SettingOut(**setting_shown(s, (server_chosen or {}) if s.scope == SERVER else (chosen or {}),
                                                    editable=server_admin if s.scope == SERVER else admin))
                         for s in m.settings if s.scope == ACCOUNT or server_admin]})
    return out


def _server_admin(request: Request) -> bool:
    return getattr(request.state, "server_admin", False) is True


def _server_chosen(plugin_id: str) -> dict:
    with get_control_session() as control:
        return PluginSettingsRepository(control, SERVER).get(plugin_id)


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
    said, server_admin = _said(), _server_admin(request)
    account = PluginSettingsRepository(session, ACCOUNT)
    return PluginList(plugins=[
        _out(e, rows.get(e.spec.plugin_id), accepted.get(e.spec.plugin_id), admin=_admin(request), said=said,
             server_admin=server_admin,
             chosen=account.get(e.spec.plugin_id) if e.installed else None,
             server_chosen=_server_chosen(e.spec.plugin_id) if e.installed and server_admin else None)
        for e in entries()])


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
    by = _by(request)
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
    return _out(entry, repo.rows().get(plugin_id), repo.accepted().get(plugin_id), admin=True, said=_said(),
                server_admin=_server_admin(request),
                chosen=PluginSettingsRepository(session, ACCOUNT).get(plugin_id) if entry.installed else None,
                server_chosen=_server_chosen(plugin_id) if entry.installed and _server_admin(request) else None)


def _installed(plugin_id: str) -> Entry:
    """An installed plug-in (404 otherwise: Lumiverb's own have their settings where they're used)."""
    entry = find(plugin_id)
    if entry is None or entry.installed is None:
        raise HTTPException(status_code=404, detail="No such installed plug-in")
    return entry


def _check(entry: Entry, scope: str, before: dict, body: SettingsChange, other: dict) -> tuple[dict, str | None]:
    """The values to save, after its schema's check and, when it checks them itself, the plug-in's; 422
    invalid_settings with each error by key otherwise. Also why the plug-in's check didn't run, if it didn't."""
    m = entry.installed.manifest
    chosen, errors = changed(m, scope, before, body.settings)
    if errors:
        raise ApiError(422, "invalid_settings", "Some settings aren't ones it takes: " + "; ".join(
            f"{k}: {v}" for k, v in errors.items()), {"errors": errors})
    if not m.check_settings:
        return chosen, None
    data = Path(get_settings().data_dir)
    if ((_said() or {}).get(m.plugin_id) or {}).get("state") != READY:
        return chosen, f"{m.title} isn't running: its own check didn't run"
    every = values(m, chosen if scope == ACCOUNT else other, chosen if scope == SERVER else other)
    try:
        answer = ask(socket_path(data, m.plugin_id), "POST", "/v1/settings/check",
                     body={"scope": scope, "settings": every})
    except PluginAnswerError as exc:
        return chosen, f"{m.title} didn't answer its check ({exc})"
    said = answer.get("errors") or {}
    if not isinstance(said, dict):
        return chosen, f"{m.title} answered its check with what the protocol doesn't say"
    theirs = {k: str(v)[:300] for k, v in said.items() if isinstance(k, str) and m.setting(k) is not None
              and k in body.settings}
    if theirs:
        raise ApiError(422, "invalid_settings", f"{m.title} says: " + "; ".join(
            f"{k}: {v}" for k, v in theirs.items()), {"errors": theirs, "by_plugin": True})
    return chosen, None


def _by(request: Request) -> str | None:
    return getattr(request.state, "user_id", None) or (
        f"key:{request.state.key_id}" if getattr(request.state, "key_id", None) else None)


@router.put("/{plugin_id}/settings", response_model=PluginOut, dependencies=[Depends(require_tenant_admin)])
def set_plugin_settings(
    plugin_id: str,
    body: SettingsChange,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> PluginOut:
    """An installed plug-in's settings for this account: each one named, a value or null (its default).
    422 invalid_settings (details.errors by key) for any its schema refuses, or it does. A change of a
    setting that remakes (its work's) asks what becomes of what the old settings made, as a producer's
    settings do: 409 redo_on_change with the count until redo says."""
    from src.server.api.routers.producers import what_becomes_of_the_made
    from src.server.plugin_producers import artifact_of

    entry = _installed(plugin_id)
    repo = PluginSettingsRepository(session, ACCOUNT)
    chosen, unchecked = _check(entry, ACCOUNT, repo.get(plugin_id), body, _server_chosen(plugin_id))
    artifact = artifact_of(plugin_id)
    work = artifact in PRODUCERS and entry.installed.manifest.work is not None
    if work:
        lineage.lock_settings(session, artifact)  # one change at a time
        before = lineage.desired(session, artifact)
    repo.save(plugin_id, chosen, by=_by(request), now=utcnow())
    if work:
        what_becomes_of_the_made(session, artifact, before, lineage.desired(session, artifact), body.redo, user_id)
    session.commit()
    rows, accepted = PluginRepository(session).rows(), PluginRepository(session).accepted()
    server_admin = _server_admin(request)
    out = _out(entry, rows.get(plugin_id), accepted.get(plugin_id), admin=True, said=_said(),
               server_admin=server_admin, chosen=chosen,
               server_chosen=_server_chosen(plugin_id) if server_admin else None)
    return out.model_copy(update={"unchecked": unchecked})


@router.put("/{plugin_id}/server-settings", response_model=PluginOut,
            dependencies=[Depends(require_server_admin)])
def set_plugin_server_settings(
    plugin_id: str,
    body: SettingsChange,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> PluginOut:
    """An installed plug-in's settings for the whole server (the server admin's): as an account's."""
    entry = _installed(plugin_id)
    with get_control_session() as control:
        repo = PluginSettingsRepository(control, SERVER)
        chosen, unchecked = _check(entry, SERVER, repo.get(plugin_id), body,
                                   PluginSettingsRepository(session, ACCOUNT).get(plugin_id))
        repo.save(plugin_id, chosen, by=_by(request), now=utcnow())
        control.commit()
    rows, accepted = PluginRepository(session).rows(), PluginRepository(session).accepted()
    out = _out(entry, rows.get(plugin_id), accepted.get(plugin_id), admin=_admin(request), said=_said(),
               server_admin=True, chosen=PluginSettingsRepository(session, ACCOUNT).get(plugin_id),
               server_chosen=chosen)
    return out.model_copy(update={"unchecked": unchecked})
