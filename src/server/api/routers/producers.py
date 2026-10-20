"""Producers API (ADR-016 phases 3 and 4): each producer, the settings it
makes its artifact with now, and how many clips' artifacts are current,
stale, missing or failing. The scheduler reads the settings here and makes
artifacts with them, so what it records in lineage is what's current.

Redo on change (Robert, Oct 9): changing a setting is the approval, so a
producer's stale clips are made again after anything missing, everywhere.
An admin can stop that for a producer and resume it.

Pausing (Robert, Oct 9): one switch per processing action (Upkeep, each
producer the scheduler makes), or all of them, paused and resumed on its
own (nothing else lifts a pause), with a scope: its work (nothing of it
starts) or, for a producer, its redo alone (its stale clips wait). Scans
are paused per library (ADR-018 phase 1: POST /v1/libraries/scans/pause);
Pause all pauses those too. The scheduler starts nothing more of what's
paused; what's running finishes.

Doing work now: POST /v1/producers/run asks the scheduler to make a
producer's (or every producer's) work now, in one library or all of them,
named, never inferred (Robert, Oct 9): failing clips are tried again at
once, and with scope redo what's made is made again."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_serializer
from sqlmodel import Session

from src.producers.names import now_called
from src.server.api.content_hashes import ContentHash, why_not
from src.server.api.dependencies import (
    get_current_user_id,
    get_tenant_session,
    require_editor,
    require_reader,
    require_signed_in,
    require_tenant_admin,
)
from src.server.api.devices import may_call
from src.server.api.errors import ConflictError
from src.server.api.library_access import edited, names, seen
from src.server.api.limits import MAX_IDS
from src.server.api.routers.libraries import CheckedIn
from src.server.reader_work import FROM_ORIGINALS
from src.server.repository import lineage
from src.shared.producers import (
    PAUSE_ALL,
    PAUSE_SCANS,
    PAUSE_UPKEEP,
    PRODUCERS,
    pause_state,
    pause_targets,
)
from src.shared.waits import HELD

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/producers", tags=["producers"])


class LineageIn(BaseModel):
    """How an artifact a write carries was made: the producer and version that
    made it, the hash of the settings it used (as GET /v1/producers gave
    them), and the content hash of the source file it was made from
    (src/shared/content_hash.py). Every machine write says (require_lineage)."""

    producer: str = Field(max_length=100)
    version: str = Field(min_length=1, max_length=50)
    settings_hash: str = Field(min_length=1, max_length=64)
    source_hash: ContentHash | None = None
    # The level it was made at (its index, lowest 0), for a producer with a
    # level setting (scenes' Detail): kept apart from the settings hash.
    level: int | None = Field(default=None, ge=0, le=31)


def lineage_dict(value: LineageIn | dict | str | None, source_hash: str | None = None) -> dict | None:
    """A request's lineage as a plain dict (a multipart form sends JSON text);
    a per-item source content hash fills in the batch's. None when absent or unreadable."""
    import json

    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else None
        except ValueError:
            return None
        if value is None:
            return None
    if isinstance(value, dict):  # a form field's JSON: held to the same shape as a JSON body's
        try:
            value = LineageIn.model_validate(value)
        except ValueError:
            return None
    if not isinstance(value, LineageIn):
        return None
    out = value.model_dump()
    if out.get("level") is None:
        out.pop("level", None)
    if source_hash:
        out["source_hash"] = source_hash
    return out


def with_source(made: dict | None, source_hash: str | None) -> dict | None:
    """A batch's lineage for one item: its own source content hash when it gives one."""
    return {**made, "source_hash": source_hash} if made is not None and source_hash else made


def require_lineage(value: LineageIn | dict | str | None, artifact: str) -> dict:
    """A machine write's lineage, which it must give (Robert, Oct 9: the API
    doesn't take writes that don't say how they were made): 422
    lineage_required when it's absent or unreadable, 422
    lineage_wrong_producer when another kind's producer says it made it;
    422 content_hash_not_valid when its source isn't a content hash the
    server takes. Checked before anything is saved."""
    import json

    from src.server.api.errors import InvalidChoiceError

    producer = PRODUCERS[artifact]
    sent = value
    if isinstance(sent, str):
        try:
            sent = json.loads(sent) if sent.strip() else None
        except ValueError:
            sent = None
    if isinstance(sent, dict) and sent.get("source_hash") is not None \
            and (why := why_not(sent["source_hash"])) is not None:
        raise InvalidChoiceError("content_hash_not_valid", f"lineage source_hash: {why}", {"artifact": artifact})
    made = lineage_dict(value)
    if made is None:
        raise InvalidChoiceError(
            "lineage_required",
            f"A machine write must say how it was made ({producer.title.lower()}): lineage (producer, version, "
            "settings_hash), as GET /v1/producers gives them.",
            {"artifact": artifact, "expected": producer.artifact})
    if made["producer"] != producer.artifact:
        raise InvalidChoiceError(
            "lineage_wrong_producer",
            f"{made['producer']} isn't the producer of {producer.title.lower()}: {producer.artifact} is.",
            {"artifact": artifact, "sent": made["producer"], "expected": producer.artifact})
    if producer.level is not None and not (isinstance(made.get("level"), int)
                                           and made["level"] < len(producer.level.choices)):
        raise InvalidChoiceError(
            "lineage_required",
            f"A machine write of {producer.title.lower()} must say the {producer.level.label.lower()} it was"
            f" made at: lineage level, the index of one of {', '.join(producer.level.choices)}.",
            {"artifact": artifact, "expected": producer.artifact})
    return made


class ProducerCounts(BaseModel):
    applicable: int  # clips it applies to
    current: int
    stale: int  # made with another producer, version, settings or source
    missing: int  # not made yet (or only failed so far)
    rechecking: int = 0  # made, and due to be checked again (location guesses)
    # Made below the producer's level now (scenes' Detail raised since):
    # not stale and not current; made again only when asked (POST .../below/redo).
    below: int = 0
    failing: int  # the last try failed (whether or not an older artifact exists)
    given_up: int = 0  # failing, and no longer tried (after 10 tries) until someone asks


class SettingField(BaseModel):
    """A setting as Settings → Processing shows it: what it is, its bounds,
    its value now. fixed: why it can't be changed here (None: it can)."""

    key: str
    label: str
    kind: str  # "int" | "float" | "text" | "bool" | "level"
    value: Any
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    advanced: bool = False
    fixed: str | None = None
    # Changing it makes the artifact again. False: it changes only what's
    # done with what's made (how faces are grouped); it isn't in settings.
    remakes: bool = True
    # kind "level": its values, lowest first. Raising it makes nothing
    # again: what was made lower counts as below (counts.below) until a redo is asked.
    choices: list[str] = []


class ProducerItem(BaseModel):
    artifact: str  # the name of what it makes (ADR-024), a plug-in's work its plug-in's id
    version: str
    # The names it's made from, of its own clip and of other clips (docs/data-names.md).
    reads: list[str] = []
    reads_others: list[str] = []
    title: str
    media: list[str]
    # All-or-nothing: its output must come from one model across the library.
    uniform: bool
    settings: dict[str, Any]
    settings_hash: str
    fields: list[SettingField] = []
    counts: ProducerCounts | None = None
    # Made by the scheduler; false: by scans (proxies, video previews), which
    # aren't paused one by one: pausing scans (or all processing) stops them.
    scheduled: bool = True
    # Whether its stale artifacts are made again (after anything missing),
    # and if they can't be yet, why.
    redoable: bool = True
    why_not: str | None = None
    # An admin stopped its redo, or a new version of it did (new_version):
    # stale clips wait until it's resumed (or its settings change). What's
    # missing is still made.
    redo_stopped: bool = False
    redo_stopped_by: str | None = None  # admins only
    redo_stopped_at: datetime | None = None
    # An admin paused it: nothing more of it starts until it's resumed (what's running finishes).
    paused: bool = False
    paused_by: str | None = None  # admins only
    paused_at: datetime | None = None
    # Why its work waits now (its AI job has no machine, none online, or is
    # off), in a sentence: waits says it too (ai_machines), with every other reason.
    waiting: str | None = None
    # What its work grows with: "second" (of video) or "clip"; time left is reckoned in it.
    unit: str = "clip"
    # Its level now (its level setting's index, lowest 0); None without one.
    level: int | None = None
    # Shown as a part of another producer (its artifact): scenes are part of scene descriptions.
    part_of: str | None = None
    # The artifacts it's made from: a clip that lacks one waits for it (and
    # isn't counted here: scene descriptions apply to videos with scenes).
    needs: list[str] = []
    # Its redo was stopped by a new version of it (not an admin): what an
    # older version made is made again once an admin resumes it.
    new_version: bool = False
    # Why its due work waits now, each reason with how many clips
    # (src/server/producer_waits.py); None without counts.
    waits: list[Wait] | None = None
    # The plug-in it belongs to (ADR-021): {plugin_id, title, on}; it applies only while that's on here.
    plugin: dict[str, Any] | None = None


class Wait(BaseModel):
    """Why some of a producer's due work waits now: a reason from
    src/shared/waits.py, how many clips, and what's needed to say it (only
    the fields its reason has are sent)."""

    reason: str
    clips: int
    # needs: the artifact it waits for.
    artifact: str | None = None
    # needs: what makes that is paused (a library's scans, for what scans make);
    # scan: a library it waits on a scan of has its scans paused.
    paused: bool | None = None
    # storage: the library, and what the scheduler's looks at its drive found.
    library_id: str | None = None
    library_name: str | None = None
    seen_at: datetime | None = None
    away_since: datetime | None = None
    checked: bool | None = None
    # ai_machines: the job, and why: "off", "no_machine", "offline" or
    # "unusable" (the scheduler can't use them: message says why).
    job: str | None = None
    # ai_machines (above); scheduler: "not_running", "starting" or "cant_list" (message: the error).
    why: str | None = None
    message: str | None = None
    # device: the device reading the library (library_id, library_name above), when it last checked in for it,
    # and why it holds its remakes back then ("battery", "metered"; ADR-018 decision 6).
    device_id: str | None = None
    device_name: str | None = None
    checked_in_at: datetime | None = None
    remakes_held: str | None = None

    @model_serializer(mode="wrap")
    def _what_it_says(self, handler: Any) -> dict[str, Any]:
        return {k: v for k, v in handler(self).items() if v is not None}


ProducerItem.model_rebuild()


class ProducerList(BaseModel):
    producers: list[ProducerItem]


def tenant_ai(request: Request) -> tuple[dict[str, str], dict[str, Any]]:
    """The tenant's model per AI job (Settings → AI), and why each job's
    work waits now (an ai_machines.JobWait; None: it doesn't), from the control plane."""
    from src.server.database import get_control_session
    from src.server.repository.ai_machines import job_models, why_jobs_wait
    from src.server.repository.control_plane import TenantRepository

    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        return {}, {}
    with get_control_session() as ctrl:
        tenant = TenantRepository(ctrl).get_by_id(tenant_id)
        return job_models(tenant), why_jobs_wait(ctrl, tenant_id)


@router.get("", response_model=ProducerList, dependencies=[Depends(require_signed_in)])
@names(sees=("query.library_id",))
def list_producers(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    library_id: str | None = None,
    project_id: str | None = None,
    counts: bool = True,
) -> ProducerList:
    """Every producer with its settings now, whether it's paused and whether
    its redo is stopped; counts, and why its due work waits (in one library
    or project when given), unless counts=false, as the scheduler asks."""
    from src.server.producer_waits import producer_waits

    models, waits = tenant_ai(request)
    asset_ids = None
    if counts and project_id:
        require_editor(request)  # like every project route
        asset_ids = _project_clips(request, session, user_id, project_id)
    admin = _admin(request)
    paused = lineage.pauses(session)
    items, wants, counted = [], {}, {}
    visible = seen(request, session)  # counted over the libraries a person can see (ADR-018 decision 8)
    for artifact in PRODUCERS:
        want = wants[artifact] = lineage.desired(session, artifact, models)
        item = _with_state(_item(artifact, want, waits, lineage.uses(session, artifact)), paused, admin)
        if item.plugin:
            from src.server.repository.plugins import PluginRepository

            item.plugin["on"] = PluginRepository(session).is_on(item.plugin["plugin_id"])
        if counts:
            counted[artifact] = lineage.counts(session, artifact, want, library_id, asset_ids, within=visible)
            item.counts = ProducerCounts(**counted[artifact])
        items.append(item)
    if counts:
        # What one producer waits for is another's count: every count first.
        status = read_status(session)
        for item in items:
            p = PRODUCERS[item.artifact]
            item.waits = [Wait(**w) for w in producer_waits(
                session, item.artifact, wants[item.artifact], counts=counted, paused=paused, status=status,
                ai=waits.get(p.job) if p.job else None, library_id=library_id, asset_ids=asset_ids,
                within=visible)]
    return ProducerList(producers=items)


def _admin(request: Request) -> bool:
    return getattr(request.state, "role", None) == "admin"


def _with_state(item: ProducerItem, paused: lineage.Pauses, admin: bool) -> ProducerItem:
    """Whether its redo is stopped (by an admin, or a new version of it) and
    whether it's paused (who did it: admins only)."""
    if (redo := paused.redo.get(item.artifact)) is not None:
        item.redo_stopped, item.redo_stopped_at = True, redo["paused_at"]
        item.redo_stopped_by = redo["paused_by"] if admin else None
        item.new_version = redo["paused_by"] is None  # lineage.hold_new_version
    if (pause := paused.work.get(item.artifact)) is not None:
        item.paused, item.paused_at = True, pause["paused_at"]
        item.paused_by = pause["paused_by"] if admin else None
    return item


def _item(artifact: str, want: dict[str, Any], waits: dict[str, Any], uses: dict[str, Any]) -> ProducerItem:
    p = PRODUCERS[artifact]
    values = {**want["settings"], **want.get("notches", {}), **uses}  # a notch as chosen, not what it stands for
    installed = bool(p.plugin)  # an installed plug-in's work
    job_wait = waits.get(p.job) if p.job else None
    return ProducerItem(
        artifact=artifact, version=p.version, title=p.title, media=list(p.media),
        reads=list(p.reads), reads_others=list(p.reads_others),
        uniform=p.uniform, settings=want["settings"], settings_hash=want["settings_hash"],
        fields=[SettingField(key=s.key, label=s.label, kind=s.kind, value=values.get(s.key, s.default),
                             default=s.default, minimum=s.minimum, maximum=s.maximum, unit=s.unit,
                             advanced=s.advanced, fixed=(SET_IN_PLUGINS if installed else s.fixed) or None,
                             remakes=s.remakes, choices=list(s.choices))
                for s in p.settings],
        scheduled=p.scheduled, redoable=lineage.redoable(artifact), why_not=lineage.CANT_REDO.get(artifact),
        waiting=job_wait.message if job_wait else None, unit=p.unit, level=want.get("level"),
        part_of=p.part_of or None, needs=list(p.needs), plugin=_plugin(p.plugin),
    )


def _plugin(plugin_id: str) -> dict[str, Any] | None:
    """The plug-in a producer belongs to, for its row: its id and title (on: whether it's on, added where
    the account is at hand)."""
    from src.server.plugin_catalog import find

    entry = find(plugin_id) if plugin_id else None
    return {"plugin_id": entry.spec.plugin_id, "title": entry.spec.title} if entry else None


# Why an installed plug-in's work's settings aren't changed on its row: they're the plug-in's.
SET_IN_PLUGINS = "Set in Settings › Plug-ins"


def what_becomes_of_the_made(session: Session, artifact: str, before: dict[str, Any], after: dict[str, Any],
                             redo: bool | None, user_id: str | None) -> None:
    """New settings for a producer (what it's made with, before and after: lineage.desired) ask what
    becomes of what the old ones made (ADR-018 decision 6): 409 redo_on_change with the count (question
    "remake"), the session rolled back, until redo says. True remakes them too, after everything else (its
    redo resumed if it was stopped); False is new files only: they stay as they are, its redo stopped by
    this person (lineage.keep_made), until someone remakes them. Nothing made yet: nothing asked, its redo
    resumed. Settings that make nothing another way ask nothing. Doesn't commit."""
    from src.server.api.errors import DecisionRequiredError

    if after["settings_hash"] == before["settings_hash"]:
        return
    p = PRODUCERS[artifact]
    clips = lineage.would_redo(session, artifact, after)
    if clips and redo is None:
        paused = lineage.pauses(session)
        stopped, held = artifact in paused.redo, artifact in paused.work
        session.rollback()
        also = " and ".join(PRODUCERS[a].title.lower() for a in lineage.made_from(artifact)[1:])
        one = clips == 1
        raise DecisionRequiredError(
            "redo_on_change",
            f"{clips:,} clip{'' if one else 's'} of {p.title.lower()} {'was' if one else 'were'} made with the "
            "old settings. New files only, and they stay as they are until you remake them; or remake them "
            "too" + (f", and their {also} with them" if also else "") + ", after everything else: until "
            "that's done, results mix the old settings and the new."
            + (f" {p.redo_note}" if p.redo_note else "")
            + (" Its stopped redo starts again if they're remade." if stopped else "")
            + (" It's paused: none of it is made until it's resumed." if held else ""),
            {"artifact": artifact, "clips": clips, "redo_stopped": stopped, "paused": held,
             "question": "remake", "artifacts": [{"artifact": artifact, "title": p.title, "clips": clips}]},
        )
    if clips and redo is False:
        lineage.keep_made(session, [artifact], by=user_id)
    else:
        lineage.resume(session, [artifact], lineage.REDO)


class SettingsIn(BaseModel):
    # key → value; null puts a setting back to its default. Others stay as they are.
    settings: dict[str, Any]
    # What becomes of what the old settings made (asked with 409 redo_on_change, ADR-018 decision 6): true,
    # remade too, after everything else; false, new files only (it stays as it is until someone remakes it).
    # Left out: asked.
    redo: bool | None = None


@router.put("/{artifact}/settings", response_model=ProducerItem, dependencies=[Depends(require_tenant_admin)])
def set_settings(
    artifact: str,
    body: SettingsIn,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> ProducerItem:
    """Change a producer's output-affecting settings (admins). Only settings
    its code reads can be changed: 422 unknown_setting, setting_fixed (why,
    in the message) or bad_setting (out of bounds, the wrong kind). What the
    old settings made is then made another way than now, and the change
    asks what becomes of it (ADR-018 decision 6), as a model change in
    Settings → AI does: 409 redo_on_change with the count (question
    "remake") until redo says. true remakes them too, after everything else
    (its redo resumed if it was stopped); false is new files only: they stay
    as they are, its redo stopped by this person (lineage.keep_made), until
    someone remakes them. Nothing made yet: nothing asked, its redo resumed. A
    pause stays. A setting that doesn't remake (how faces are grouped) asks
    nothing and makes nothing again: when one changes, the producer's
    regroup runs (the face groups are worked out again; location guesses
    are checked again)."""
    from src.producers import load
    from src.server.api.errors import InvalidChoiceError

    p = producer_or_404(artifact)
    if p.plugin:
        raise InvalidChoiceError("setting_fixed", f"{p.title}: {SET_IN_PLUGINS}", {"plugin_id": p.plugin})
    lineage.lock_settings(session, artifact)  # one change at a time per producer
    values = dict(lineage.overrides(session, artifact))
    for key, value in body.settings.items():
        setting = p.setting(key)
        if setting is None:
            raise InvalidChoiceError("unknown_setting", f"{p.title} has no setting {key!r}", {"key": key})
        if setting.fixed:
            raise InvalidChoiceError("setting_fixed", f"{setting.label}: {setting.fixed}", {"key": key})
        try:
            checked = None if value is None else setting.check(value)
        except ValueError as e:
            raise InvalidChoiceError("bad_setting", str(e), {"key": key}) from None
        if checked is None or checked == setting.default:
            values.pop(key, None)
        else:
            values[key] = checked
    models, waits = tenant_ai(request)
    before, used = lineage.desired(session, artifact, models), lineage.uses(session, artifact)
    lineage.set_overrides(session, artifact, values)
    after, uses = lineage.desired(session, artifact, models), lineage.uses(session, artifact)
    what_becomes_of_the_made(session, artifact, before, after, body.redo, user_id)
    if uses != used and p.regroup:
        load(p.regroup)(session, used, uses)
    session.commit()
    # Settings unchanged leave a stopped redo stopped.
    return _with_state(_item(artifact, after, waits, uses), lineage.pauses(session), admin=True)


def _project_clips(request: Request, session: Session, user_id: str, project_id: str) -> list[str]:
    """Every clip in a project the caller can see; 404 otherwise."""
    from src.server.api.routers.projects import _all_project_assets, _can_view
    from src.server.repository.projects import ProjectRepository

    repo = ProjectRepository(session, within=seen(request, session))
    project = repo.get_by_id(project_id)
    if project is None or getattr(project, "deleted_at", None) is not None or not _can_view(project, user_id, repo):
        raise HTTPException(status_code=404, detail="Project not found")
    return [a.asset_id for a in _all_project_assets(project, request, session, user_id)]


SWITCH_TITLES = {PAUSE_UPKEEP: "Upkeep"}


class PauseIn(BaseModel):
    # "work": all of the switch's work (nothing of it starts, missing or
    # stale); "redo": a producer's redo alone (its stale clips wait; what's
    # missing is still made). Named, never inferred.
    scope: Literal["work", "redo"]


def _targets(target: str, scope: str) -> list[str]:
    """What a pause names: one switch (Upkeep or a producer the scheduler
    makes) or every one ("all", named, never a missing target). 404 for no
    such producer; 409 per_library for "scans" (paused per library since
    ADR-018 phase 1); 409 not_scheduled for the work of what scans make (a
    library's Scans pause stops it), though their remakes are a redo. A redo
    is a producer's: 422 bad_scope for Upkeep, 409 cant_redo for a producer
    that isn't made again yet; "all" names every producer that is."""
    from src.server.api.errors import InvalidChoiceError

    if target == PAUSE_SCANS:
        raise ConflictError("per_library", "Scans are paused for each library: "
                            "POST /v1/libraries/scans/pause with the libraries, or all.", {"target": target})
    if target == PAUSE_ALL:
        if scope == lineage.WORK:
            return list(pause_targets())
        return [a for a in PRODUCERS if lineage.redoable(a)]  # scans' remakes too
    if target in SWITCH_TITLES:
        if scope == lineage.REDO:
            raise InvalidChoiceError("bad_scope", f"{SWITCH_TITLES[target]} make nothing again: pause its work.",
                                     {"target": target})
        return [target]
    p = producer_or_404(target)
    if scope == lineage.REDO and not lineage.redoable(target):
        raise ConflictError("cant_redo", f"{p.title} isn't made again yet. {lineage.CANT_REDO.get(target, '')}".strip())
    if not p.scheduled and scope != lineage.REDO:  # what scans make: their remakes stop or go on (scans.py)
        raise ConflictError("not_scheduled", f"{p.title} are made by scans: pausing a library's scans stops them.",
                            {"artifact": target})
    return [target]


@router.post("/{target}/pause", status_code=204, dependencies=[Depends(require_tenant_admin)])
def pause_switch(
    target: str,
    body: PauseIn,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> None:
    """Pause one switch (admins), or "all", whatever the others are. Scope
    work: upkeep (no trash purge, file cleanup or face names spread; search
    sync goes on) or a producer (nothing more of it starts, missing or
    stale); all is every switch and every library's scans (state paused;
    each can be resumed alone after; "all" is stored, so a producer a later
    version adds is paused too). A library's scans alone: POST
    /v1/libraries/scans/pause. Scope redo: a producer's
    stale clips stay as they are until it's resumed (Remake), or a change of
    its settings or model says to remake them (what's missing is still made). What's running finishes. Again is
    fine: who paused first stays."""
    if target == PAUSE_ALL and body.scope == lineage.WORK:
        lineage.pause_everything(session, by=user_id)
    else:
        for name in _targets(target, body.scope):
            lineage.pause(session, name, body.scope, by=user_id)
    session.commit()


@router.post("/{target}/resume", status_code=204, dependencies=[Depends(require_tenant_admin)])
def resume_switch(target: str, body: PauseIn, session: Annotated[Session, Depends(get_tenant_session)]) -> None:
    """Resume one switch (admins), or "all", in that scope, whatever the others are."""
    if target == PAUSE_ALL and body.scope == lineage.WORK:
        lineage.resume_everything(session)  # a row for what's no longer a switch goes too
    elif body.scope == lineage.REDO and target != PAUSE_ALL and target in PRODUCERS:
        lineage.resume(session, [target], body.scope)  # always: a leftover stop is never stuck
    else:
        lineage.resume(session, _targets(target, body.scope), body.scope)
    session.commit()


class Failure(BaseModel):
    asset_id: str = Field(min_length=1, max_length=64)
    artifact: str = Field(max_length=100)
    error: str = Field(max_length=10_000)

    @field_validator("artifact")
    @classmethod
    def _a_producer(cls, value: str) -> str:
        # As the producers are now: a plug-in's work can be added while Lumiverb runs. By its name.
        if value not in PRODUCERS:
            raise ValueError(_not_a_producer(value))
        return value


class FailuresIn(BaseModel):
    items: list[Failure] = Field(max_length=500)


@router.post("/failures", dependencies=[Depends(require_reader)])
@may_call("items", artifacts=FROM_ORIGINALS)
@names("body.items[].asset_id")
def report_failures(body: FailuresIn, session: Annotated[Session, Depends(get_tenant_session)]) -> dict:
    """The scheduler couldn't make these, or a device what it makes from its
    originals (for the libraries it reads): each is kept with its error and
    not handed out (nor listed in reader-work) again for 5 minutes, then 10,
    20 and so on up to a day, and given up after 10 tries until someone asks
    for it to be tried again. An artifact made earlier stays what it was.
    Clips that don't exist are left out. Returns {"recorded"}."""
    from src.server.repository.assets import AssetRepository

    known = AssetRepository(session).existing_ids(list({f.asset_id for f in body.items}))
    recorded = 0
    for f in body.items:
        if f.asset_id in known:
            lineage.record_failure(session, f.asset_id, f.artifact, f.error or "failed", commit=False)
            recorded += 1
    session.commit()
    return {"recorded": recorded}


class PauseSwitch(BaseModel):
    target: str  # "upkeep" or a producer's artifact
    title: str
    paused: bool = False
    paused_by: str | None = None  # admins only
    paused_at: datetime | None = None
    # Why its work waits, as a producer's row says it (src/shared/waits.py).
    waits: list[dict[str, Any]] = Field(default_factory=list)


class LibraryScans(BaseModel):
    """A library's scans (ADR-018 phase 1): who reads its files
    (src/shared/readers.py), and its own Scans pause."""

    library_id: str
    name: str
    reader: str
    # The reader device's name (None: this server), and its latest check-in for the library (ADR-018 decision 1).
    reader_name: str | None = None
    checked_in: CheckedIn | None = None
    paused: bool = False
    paused_by: str | None = None  # admins only
    paused_at: datetime | None = None
    # Why its scans wait (src/shared/waits.py): its scans keep failing
    # (producer_waits.scan_waits).
    waits: list[dict[str, Any]] = Field(default_factory=list)


class StorageSeen(BaseModel):
    """What the scheduler's looks at one library's storage found."""

    seen_at: datetime | None = None  # last seen reachable
    away_since: datetime | None = None  # unreachable since (None: the latest look reached it)
    checked: bool = False  # looked at since the scheduler started


class HeldClips(BaseModel):
    """Clips the scheduler holds back after a try that waited (for up to an
    hour), of one kind (a redo's under its kind) and library, and why
    (src/shared/waits.py: "scan" or "held"; one without a code is held)."""

    kind: str
    why: str = HELD
    library_id: str = ""
    clips: int = 0


class ScanFailing(BaseModel):
    """A library whose scans keep failing (the scan itself, not a file): the
    latest error, since when, and when it's tried again."""

    library_id: str
    error: str = ""
    since: datetime | None = None
    next_try: datetime | None = None


class SchedulerStatus(BaseModel):
    """What the scheduler is doing for the account (written every few seconds)."""

    # It wrote in the last 30 seconds: it's running.
    live: bool = False
    at: datetime | None = None
    running: dict[str, int] = Field(default_factory=dict)  # jobs running now, by kind
    waiting: dict[str, int] = Field(default_factory=dict)  # jobs lined up next, by kind
    pools: dict[str, list[int]] = Field(default_factory=dict)  # each pool's [busy, slots]
    # Requests the AI machines sharing its GPU give up while video is decoded there.
    gpu_hold: int = 0
    # The account's pause state, derived from its switches and its libraries'
    # scans (Robert, Oct 9): "running" (none paused: green), "partly" (some:
    # yellow), "paused" (all: red).
    state: Literal["running", "partly", "paused"] = "running"
    # Every pause switch: Upkeep, then each producer the scheduler makes.
    switches: list[PauseSwitch] = Field(default_factory=list)
    # Each library (not in the trash): who reads it, and its Scans pause.
    libraries: list[LibraryScans] = Field(default_factory=list)
    # Seconds a slot spends per unit of work, by kind (learned from finished jobs).
    pace: dict[str, float] = Field(default_factory=dict)
    # Jobs running now: {kind, units, elapsed}.
    jobs: list[dict[str, Any]] = Field(default_factory=list)
    # Libraries whose work on the originals waits: not reachable at the latest
    # look, or not looked at since the scheduler started (not "can't reach":
    # storage says which).
    storage_waits: list[str] = Field(default_factory=list)
    # Each library's storage looks: {seen_at, away_since, checked}. checked:
    # looked at since the scheduler started; otherwise a record from before.
    storage: dict[str, StorageSeen] = Field(default_factory=dict)
    # Clips it holds back after a try that waited, by kind, why and library.
    held: list[HeldClips] = Field(default_factory=list)
    # AI jobs whose machines it can't use now, and why (their work isn't handed out).
    ai_waits: dict[str, str] = Field(default_factory=dict)
    # Installed plug-ins' work whose plug-in isn't running, and why, by artifact (not handed out).
    plugin_waits: dict[str, str] = Field(default_factory=dict)
    # It has read the account's settings: until then it hands out nothing.
    settings_ready: bool = True
    # Kinds whose last listing failed, and the error (their work isn't handed out).
    unlisted: dict[str, str] = Field(default_factory=dict)
    # Libraries whose scans keep failing: not scanned again until next_try.
    scans_failing: list[ScanFailing] = Field(default_factory=list)
    # How long until things are made (src/server/scheduler/eta.py); None when it isn't running.
    eta: Eta | None = None


class JobLeft(BaseModel):
    kind: str
    artifact: str | None  # the producer whose job it is
    unit: str = "clip"  # the producer's: "second" (of video) or "clip"
    units: float  # its work, in that unit
    elapsed: float  # seconds it has run
    left: float | None  # seconds left at its pace; None: no pace yet
    late: bool = False  # well past its pace


class NotCounted(BaseModel):
    artifact: str
    title: str
    why: str  # "no_machine" (none doing its work now) | "not_known_yet" (no pace yet) | "paused" (an admin paused it)


class Eta(BaseModel):
    # Seconds until each producer, each pool and everything is caught up.
    # A producer with work and no time is in not_counted, saying why, and
    # caught_up leaves it out (None: nothing with work is counted).
    producers: dict[str, float | None]
    pools: dict[str, float]
    caught_up: float | None
    not_counted: list[NotCounted] = []
    jobs: list[JobLeft]


SchedulerStatus.model_rebuild()


@router.get("/queue", response_model=SchedulerStatus, dependencies=[Depends(require_signed_in)])
def scheduler_status(request: Request, session: Annotated[Session, Depends(get_tenant_session)]) -> SchedulerStatus:
    """What the scheduler is doing now, and each pause switch with the state they make;
    live is false when it hasn't said for 30 seconds. While it's live, eta says
    how long until each producer and everything is caught up, from each kind's
    pace and what's left; paused work gets no time."""
    from src.server.producer_waits import scan_waits
    from src.server.scheduler.eta import eta
    from src.shared.utils import utcnow

    now = utcnow()
    status = read_status(session, now)
    paused, admin = lineage.pauses(session), _admin(request)
    held = paused.work
    from src.server import check_ins
    from src.server.api.routers.libraries import checked_in_of
    from src.server.repository.devices import DeviceRepository
    from src.server.repository.libraries import LibraryRepository

    visible = seen(request, session)  # what a person can see of the libraries (ADR-018 decision 8)
    libraries = [lib for lib in LibraryRepository(session).active_brief() if visible is None or lib[0] in visible]
    checked, names = check_ins.latest(session), DeviceRepository(session).names()
    failing = scan_waits(session, status, visible)
    status.state = pause_state(set(held), libraries_paused=sum(1 for lib in libraries if paused.scans_paused(lib[0])),
                               libraries=len(libraries))
    status.switches = [
        PauseSwitch(target=t, title=SWITCH_TITLES.get(t) or PRODUCERS[t].title, paused=t in held,
                    paused_at=held[t]["paused_at"] if t in held else None,
                    paused_by=held[t]["paused_by"] if t in held and admin else None)
        for t in pause_targets()]
    status.libraries = [
        LibraryScans(library_id=library_id, name=name, reader=reader, reader_name=names.get(reader),
                     checked_in=checked_in_of(checked.get(library_id)), paused=paused.scans_paused(library_id),
                     paused_at=paused.libraries.get(library_id, {}).get("paused_at"),
                     paused_by=paused.libraries.get(library_id, {}).get("paused_by") if admin else None,
                     waits=[w for w in failing if w["library_id"] == library_id])
        for library_id, name, reader in libraries]
    # Paused work has no time left (Robert, Oct 9); what's running still says its time left, since it finishes.
    if status.live:
        held_producers = {t for t in held if t in PRODUCERS}
        try:
            status.eta = Eta(**eta(status.model_dump(), _work_left(request, session, status.storage_waits), now=now,
                                   paused=held_producers))
        except Exception:  # noqa: BLE001 — the time left is extra; what's running is still said
            logger.exception("producers: working out how long is left failed")
    if visible is not None:
        status.storage_waits = [i for i in status.storage_waits if i in visible]
        status.storage = {i: v for i, v in status.storage.items() if i in visible}
        status.held = [h for h in status.held if h.library_id in visible]
        status.scans_failing = [f for f in status.scans_failing if f.library_id in visible]
    return status


def read_status(session: Session, now: datetime | None = None) -> SchedulerStatus:
    """The scheduler's status as it last wrote it (scheduler.status), live
    when that was in the last 30 seconds; an empty one when there's none."""
    import json
    from datetime import timedelta

    from src.server.repository.system_metadata import SystemMetadataRepository
    from src.shared.utils import utcnow

    raw = SystemMetadataRepository(session).get_value("scheduler.status")
    status = SchedulerStatus()
    if raw:
        try:
            status = SchedulerStatus(**json.loads(raw))
        except (ValueError, TypeError):
            pass
    now = now or utcnow()
    status.live = status.at is not None and now - status.at < timedelta(seconds=30)
    return status


# Work left is counted over every clip: once in a while per account, not every poll.
WORK_LEFT_EVERY_SEC = 30.0
_work_left_cache: dict[str, tuple[float, dict[str, float]]] = {}


def _work_left(request: Request, session: Session, away: list[str]) -> dict[str, float]:
    """Each scheduled producer's work left in its unit: what the scheduler
    will make (lineage.work_left), counted at most every WORK_LEFT_EVERY_SEC."""
    import time

    tenant_id = getattr(request.state, "tenant_id", None) or ""
    cached = _work_left_cache.get(tenant_id)
    if cached and time.monotonic() - cached[0] < WORK_LEFT_EVERY_SEC:
        return cached[1]
    from src.server.repository.ai_machines import account_job_models

    models = account_job_models(tenant_id)
    stopped = lineage.pauses(session).redo
    out = {}
    for artifact, p in PRODUCERS.items():
        if not p.scheduled:
            continue
        left = lineage.work_left(session, artifact, lineage.desired(session, artifact, models),
                                 redo=lineage.redoable(artifact) and artifact not in stopped, away=away)
        out[artifact] = left["seconds"] if p.unit == "second" else left["clips"]
    _work_left_cache[tenant_id] = (time.monotonic(), out)
    return out



class FailingClip(BaseModel):
    asset_id: str
    artifact: str
    title: str  # the producer's
    rel_path: str
    library_id: str
    library_name: str
    media_type: str
    error: str
    attempts: int
    failed_at: datetime | None = None
    # When it's tried again; None once it's given up.
    retry_at: datetime | None = None
    given_up: bool = False


class FailingClips(BaseModel):
    items: list[FailingClip]
    next_cursor: str | None = None


@router.get("/failures", response_model=FailingClips, dependencies=[Depends(require_signed_in)])
@names(sees=("query.library_id",))
def list_failures(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    artifact: str | None = None,
    library_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    after: str | None = None,
) -> FailingClips:
    """Clips in sight whose last try failed, newest failure first, with the
    error, how many tries and when the next one is (none once given up)."""
    if artifact is not None:
        producer_or_404(artifact)
    rows = lineage.failures(session, artifact=artifact, library_id=library_id, limit=limit + 1, after=after,
                            within=seen(request, session))
    more = len(rows) > limit
    rows = rows[:limit]
    items = [FailingClip(asset_id=r["asset_id"], artifact=r["artifact"], title=PRODUCERS[r["artifact"]].title,
                         rel_path=r["rel_path"], library_id=r["library_id"], library_name=r["library_name"],
                         media_type=r["media_type"], error=r["error"] or "", attempts=int(r["attempts"] or 0),
                         failed_at=r["failed_at"], retry_at=r["retry_at"], given_up=r["given_up"])
             for r in rows if r["artifact"] in PRODUCERS]
    return FailingClips(items=items, next_cursor=rows[-1]["cursor"] if more and rows else None)


class RetryIn(BaseModel):
    """Failing clips to try again: these (asset_ids), or every failing clip
    (all: true); a producer's (artifact) or one library's (library_id) alone
    when given."""

    asset_ids: list[str] | None = Field(default=None, min_length=1, max_length=MAX_IDS)
    all: bool = False
    artifact: str | None = Field(default=None, max_length=64)
    library_id: str | None = Field(default=None, max_length=64)

    @field_validator("artifact")
    @classmethod
    def _by_name(cls, value: str | None) -> str | None:
        if value is not None and value not in PRODUCERS and now_called(value, "artifact"):
            raise ValueError(_not_a_producer(value))  # an old word, refused since ADR-024's part 5
        return value


@router.post("/failures/retry", dependencies=[Depends(require_editor)])
@names("body.asset_ids", "body.library_id")
def retry_failures(body: RetryIn, request: Request, session: Annotated[Session, Depends(get_tenant_session)]) -> dict:
    """Try failing clips again now, given up or not; the back-off starts
    over. asset_ids, or all: true; neither or both is a 400 scope_required.
    Returns {"retried"}."""
    from src.server.api.limits import require_scope

    require_scope(body.asset_ids is not None, body.all, "asset_ids")
    if body.artifact is not None:
        producer_or_404(body.artifact)
    n = lineage.retry(session, artifact=body.artifact, library_id=body.library_id, asset_ids=body.asset_ids,
                      within=edited(request, session))
    session.commit()
    return {"retried": n}


class RunIn(BaseModel):
    """Work for the scheduler to do now: a producer's (or "all", named), in
    these libraries (library_ids) or every one (all: true), new work or a redo."""

    producer: str = Field(max_length=64)
    library_ids: list[str] | None = Field(default=None, min_length=1, max_length=MAX_IDS)
    all: bool = False
    # "new": what's missing, failing clips tried again at once; "redo": that,
    # and what's made made again (admins), as new settings would.
    scope: Literal["new", "redo"]

    @field_validator("producer")
    @classmethod
    def _by_name(cls, value: str) -> str:
        if value not in PRODUCERS and now_called(value, "artifact"):
            raise ValueError(_not_a_producer(value))  # an old word, refused since ADR-024's part 5
        return value


class RunProducer(BaseModel):
    artifact: str
    title: str
    missing: int  # clips the scheduler makes now
    redo: int  # made, to be made again (with scope redo: those asked for, and any stale)
    retried: int  # failing clips tried again now
    paused: bool = False  # its switch is paused: nothing starts until it's resumed
    redo_stopped: bool = False  # its redo is stopped: what's made waits until it's resumed


class RunOut(BaseModel):
    producers: list[RunProducer]


@router.post("/run", response_model=RunOut, dependencies=[Depends(require_editor)])
@names("body.library_ids")
def run_now(body: RunIn, request: Request, session: Annotated[Session, Depends(get_tenant_session)]) -> RunOut:
    """Ask the scheduler to do work now (what lumiverb enrich does): failing
    clips of the producers named are tried again at once and the scheduler
    looks again now; with scope redo (admins), what they've made in the
    libraries named is made again, after anything missing (a person's never
    is). It goes as the scheduler goes: by tier, within its pools, and what's
    paused waits (each producer says). library_ids, or all: true; neither
    or both is a 400 scope_required."""
    from src.server.api.limits import require_scope
    from src.server.repository.libraries import LibraryRepository

    require_scope(body.library_ids is not None, body.all, "library_ids")
    libraries: list[str | None] = [None] if body.all else list(dict.fromkeys(body.library_ids or []))
    if any(LibraryRepository(session).get_by_id(lib) is None for lib in libraries if lib):
        raise HTTPException(status_code=404, detail="Library not found")
    if body.producer == PAUSE_ALL:
        artifacts = [a for a, p in PRODUCERS.items() if p.scheduled]
    else:
        p = producer_or_404(body.producer)
        if not p.scheduled:
            raise ConflictError("not_scheduled", f"{p.title} are made by scans.", {"artifact": body.producer})
        artifacts = [body.producer]
    redo = body.scope == "redo"
    if redo:
        if not _admin(request):
            raise HTTPException(status_code=403, detail="Making things again is for admins")
        cant = [a for a in artifacts if not lineage.redoable(a)]
        if body.producer != PAUSE_ALL and cant:
            p = PRODUCERS[cant[0]]
            raise ConflictError("cant_redo", f"{p.title} isn't made again yet. {lineage.CANT_REDO.get(cant[0], '')}".strip())
        artifacts = [a for a in artifacts if a not in cant]
    models, _ = tenant_ai(request)
    paused = lineage.pauses(session)
    out = []
    within = edited(request, session)  # all: the libraries a person edits (ADR-018 decision 8)
    for artifact in artifacts:
        want = lineage.desired(session, artifact, models)
        retried = missing = stale = 0
        for library_id in libraries:
            retried += lineage.retry(session, artifact=artifact, library_id=library_id, within=within)
            if redo:
                lineage.ask_redo(session, artifact, library_id)  # admins only: every library theirs
            c = lineage.counts(session, artifact, want, library_id, within=within)
            missing, stale = missing + c["missing"], stale + c["stale"]
        out.append(RunProducer(artifact=artifact, title=PRODUCERS[artifact].title, missing=missing,
                               redo=stale if lineage.redoable(artifact) else 0, retried=retried,
                               paused=artifact in paused.work, redo_stopped=artifact in paused.redo))
    lineage.nudge(session)
    session.commit()
    return RunOut(producers=out)


class BelowIn(BaseModel):
    """Clips made below the producer's level now to make again: these
    (asset_ids; any not below are left as they are), or every one (all:
    true), in one library alone when given."""

    asset_ids: list[str] | None = Field(default=None, min_length=1, max_length=MAX_IDS)
    all: bool = False
    library_id: str | None = Field(default=None, max_length=64)


class BelowOut(BaseModel):
    redo: int  # clips to be made again, after anything missing
    paused: bool = False  # its switch is paused: nothing starts until it's resumed
    redo_stopped: bool = False  # its redo is stopped: they wait until it's resumed


@router.post("/{artifact}/below/redo", response_model=BelowOut, dependencies=[Depends(require_tenant_admin)])
@names("body.asset_ids", "body.library_id")
def redo_below(artifact: str, body: BelowIn, request: Request,
               session: Annotated[Session, Depends(get_tenant_session)]) -> BelowOut:
    """Make again what a producer made below its level now (admins): raising
    a level (scenes' Detail) makes nothing again by itself; this is how it's
    asked, for clips named (asset_ids) or all: true; neither or both is a
    400 scope_required. 409 no_level for a producer without a level setting.
    They're redone as any redo is: after anything missing, at the level
    then, held while its redo is stopped or it's paused (each said)."""
    from src.server.api.limits import require_scope

    p = producer_or_404(artifact)
    require_scope(body.asset_ids is not None, body.all, "asset_ids")
    if p.level is None or not lineage.redoable(artifact):
        raise ConflictError("no_level", f"{p.title} has no level to redo below.", {"artifact": artifact})
    models, _ = tenant_ai(request)
    want = lineage.desired(session, artifact, models)
    n = lineage.redo_below(session, artifact, want, library_id=body.library_id,
                           asset_ids=list(dict.fromkeys(body.asset_ids)) if body.asset_ids else None)
    if n:
        lineage.nudge(session)
    paused = lineage.pauses(session)
    session.commit()
    return BelowOut(redo=n, paused=artifact in paused.work, redo_stopped=artifact in paused.redo)


def producer_or_404(artifact: str):
    """The producer a path names, by its name: 404 for none, and 404 renamed, naming the name, for what one was
    called before ADR-024 (refused since its part 5)."""
    if artifact not in PRODUCERS:
        name = now_called(artifact, "artifact")
        if name:
            from src.server.api.errors import ApiError

            raise ApiError(404, "renamed", _not_a_producer(artifact), {"sent": artifact, "name": name})
        raise HTTPException(status_code=404, detail="No such artifact")
    return PRODUCERS[artifact]


def _not_a_producer(word: str) -> str:
    """Why a word names no producer: what an old one is called now (ADR-024), or none."""
    name = now_called(word, "artifact")
    return f"{word} is called {name} now: use its name." if name else f"no producer makes {word!r}"
