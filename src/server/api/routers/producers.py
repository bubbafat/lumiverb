"""Producers API (ADR-016 phases 3 and 4): each producer, the settings it
makes its artifact with now, and how many clips' artifacts are current,
stale, missing or failing. The scheduler reads the settings here and makes
artifacts with them, so what it records in lineage is what's current.

Redo on change (Robert, Oct 9): changing a setting is the approval, so a
producer's stale clips are made again after anything missing, everywhere.
An admin can stop that for a producer and resume it.

Pausing (Robert, Oct 9): one switch per processing action (Scans, Upkeep,
each producer the scheduler makes), or all of them, paused and resumed on
its own (nothing else lifts a pause), with a scope: its work (nothing of it
starts) or, for a producer, its redo alone (its stale clips wait). The
scheduler starts nothing more of what's paused; what's running finishes.

Doing work now: POST /v1/producers/run asks the scheduler to make a
producer's (or every producer's) work now, in one library or all of them,
named, never inferred (Robert, Oct 9): failing clips are tried again at
once, and with scope redo what's made is made again."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlmodel import Session

from src.server.api.dependencies import (
    get_current_user_id,
    get_tenant_session,
    require_editor,
    require_signed_in,
    require_tenant_admin,
)
from src.server.api.errors import ConflictError
from src.server.api.limits import MAX_IDS
from src.server.repository import lineage
from src.shared.producers import (
    PAUSE_ALL,
    PAUSE_SCANS,
    PAUSE_UPKEEP,
    PRODUCERS,
    pause_state,
    pause_targets,
)

logger = logging.getLogger(__name__)

PRODUCER_ARTIFACTS = tuple(PRODUCERS)

router = APIRouter(prefix="/v1/producers", tags=["producers"])


class LineageIn(BaseModel):
    """How an artifact a write carries was made: the producer and version that
    made it, the hash of the settings it used (as GET /v1/producers gave
    them), and the SHA-256 of the source file it was made from. Every
    machine write says (require_lineage)."""

    producer: str = Field(max_length=100)
    version: str = Field(min_length=1, max_length=50)
    settings_hash: str = Field(min_length=1, max_length=64)
    source_sha256: str | None = Field(default=None, max_length=128)


def lineage_dict(value: LineageIn | dict | str | None, source_sha256: str | None = None) -> dict | None:
    """A request's lineage as a plain dict (a multipart form sends JSON text);
    a per-item source SHA-256 fills in the batch's. None when absent or unreadable."""
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
    if source_sha256:
        out["source_sha256"] = source_sha256
    return out


def with_source(made: dict | None, source_sha256: str | None) -> dict | None:
    """A batch's lineage for one item: its own source SHA-256 when it gives one."""
    return {**made, "source_sha256": source_sha256} if made is not None and source_sha256 else made


def require_lineage(value: LineageIn | dict | str | None, artifact: str) -> dict:
    """A machine write's lineage, which it must give (Robert, Oct 9: the API
    doesn't take writes that don't say how they were made): 422
    lineage_required when it's absent or unreadable, 422
    lineage_wrong_producer when another kind's producer says it made it.
    Checked before anything is saved."""
    from src.server.api.errors import InvalidChoiceError

    producer = PRODUCERS[artifact]
    made = lineage_dict(value)
    if made is None:
        raise InvalidChoiceError(
            "lineage_required",
            f"A machine write must say how it was made ({producer.title.lower()}): lineage (producer, version, "
            "settings_hash), as GET /v1/producers gives them.",
            {"artifact": artifact, "expected": producer.producer})
    if made["producer"] != producer.producer:
        raise InvalidChoiceError(
            "lineage_wrong_producer",
            f"{made['producer']} isn't the producer of {producer.title.lower()}: {producer.producer} is.",
            {"artifact": artifact, "sent": made["producer"], "expected": producer.producer})
    return made


class ProducerCounts(BaseModel):
    applicable: int  # clips it applies to
    current: int
    stale: int  # made with another producer, version, settings or source
    missing: int  # not made yet (or only failed so far)
    failing: int  # the last try failed (whether or not an older artifact exists)
    given_up: int = 0  # failing, and no longer tried (after 10 tries) until someone asks


class SettingField(BaseModel):
    """A setting as Settings → Processing shows it: what it is, its bounds,
    its value now. fixed: why it can't be changed here (None: it can)."""

    key: str
    label: str
    kind: str  # "int" | "float" | "text" | "bool"
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


class ProducerItem(BaseModel):
    artifact: str
    producer: str
    version: str
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
    # An admin stopped its redo: stale clips wait until it's resumed (or its
    # settings change). What's missing is still made.
    redo_stopped: bool = False
    redo_stopped_by: str | None = None  # admins only
    redo_stopped_at: datetime | None = None
    # An admin paused it: nothing more of it starts until it's resumed (what's running finishes).
    paused: bool = False
    paused_by: str | None = None  # admins only
    paused_at: datetime | None = None
    # Why its work waits now (its AI job has no machine, none online, or is off).
    waiting: str | None = None
    # What its work grows with: "second" (of video) or "clip"; time left is reckoned in it.
    unit: str = "clip"


class ProducerList(BaseModel):
    producers: list[ProducerItem]


def tenant_ai(request: Request) -> tuple[dict[str, str], dict[str, str | None]]:
    """The tenant's model per AI job (Settings → AI), and why each job's
    work waits now (None: it doesn't), from the control plane."""
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
def list_producers(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    library_id: str | None = None,
    project_id: str | None = None,
    counts: bool = True,
) -> ProducerList:
    """Every producer with its settings now, whether it's paused and whether
    its redo is stopped; counts (in one library or project when given)
    unless counts=false, as the scheduler asks."""
    models, waits = tenant_ai(request)
    asset_ids = None
    if counts and project_id:
        require_editor(request)  # like every project route
        asset_ids = _project_clips(request, session, user_id, project_id)
    admin = _admin(request)
    paused = lineage.pauses(session)
    items = []
    for artifact in PRODUCERS:
        want = lineage.desired(session, artifact, models)
        item = _with_state(_item(artifact, want, waits, lineage.uses(session, artifact)), paused, admin)
        if counts:
            item.counts = ProducerCounts(**lineage.counts(session, artifact, want, library_id, asset_ids))
        items.append(item)
    return ProducerList(producers=items)


def _admin(request: Request) -> bool:
    return getattr(request.state, "role", None) == "admin"


def _with_state(item: ProducerItem, paused: lineage.Pauses, admin: bool) -> ProducerItem:
    """Whether its redo is stopped and whether it's paused (who did it: admins only)."""
    if (redo := paused.redo.get(item.artifact)) is not None:
        item.redo_stopped, item.redo_stopped_at = True, redo["paused_at"]
        item.redo_stopped_by = redo["paused_by"] if admin else None
    if (pause := paused.work.get(item.artifact)) is not None:
        item.paused, item.paused_at = True, pause["paused_at"]
        item.paused_by = pause["paused_by"] if admin else None
    return item


def _item(artifact: str, want: dict[str, Any], waits: dict[str, str | None], uses: dict[str, Any]) -> ProducerItem:
    p = PRODUCERS[artifact]
    values = {**want["settings"], **uses}
    return ProducerItem(
        artifact=artifact, producer=p.producer, version=p.version, title=p.title, media=list(p.media),
        uniform=p.uniform, settings=want["settings"], settings_hash=want["settings_hash"],
        fields=[SettingField(key=s.key, label=s.label, kind=s.kind, value=values.get(s.key, s.default),
                             default=s.default, minimum=s.minimum, maximum=s.maximum, unit=s.unit,
                             advanced=s.advanced, fixed=s.fixed or None, remakes=s.remakes)
                for s in p.settings],
        scheduled=p.scheduled, redoable=lineage.redoable(artifact), why_not=lineage.CANT_REDO.get(artifact),
        waiting=waits.get(p.job) if p.job else None, unit=p.unit,
    )


class SettingsIn(BaseModel):
    # key → value; null puts a setting back to its default. Others stay as they are.
    settings: dict[str, Any]
    # Yes, make again what the old settings made (asked with 409 redo_on_change).
    redo: bool = False


@router.put("/{artifact}/settings", response_model=ProducerItem, dependencies=[Depends(require_tenant_admin)])
def set_settings(
    artifact: str,
    body: SettingsIn,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> ProducerItem:
    """Change a producer's output-affecting settings (admins). Only settings
    its code reads can be changed: 422 unknown_setting, setting_fixed (why,
    in the message) or bad_setting (out of bounds, the wrong kind). New
    settings make what the old ones made stale, and it's made again after
    anything missing (Robert, Oct 9: the change is the approval): 409
    redo_on_change with the count until redo says yes, as a model change in
    Settings → AI asks. Saving resumes its redo if an admin had stopped it;
    a pause stays. A setting that doesn't remake (how faces are grouped)
    asks nothing and makes nothing again: when one changes, the producer's
    regroup runs (the face groups are worked out again)."""
    from src.server.api.errors import DecisionRequiredError, InvalidChoiceError

    p = producer_or_404(artifact)
    # One change at a time per producer: two saves at once mustn't lose either's settings.
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"producer.{artifact}"})
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
    if after["settings_hash"] != before["settings_hash"]:
        clips = lineage.would_redo(session, artifact, after)
        if clips and not body.redo:
            paused = lineage.pauses(session)
            stopped, held = artifact in paused.redo, artifact in paused.work
            session.rollback()
            also = " and ".join(PRODUCERS[a].title.lower() for a in lineage.made_from(artifact)[1:])
            raise DecisionRequiredError(
                "redo_on_change",
                f"New settings make {clips:,} clip{'' if clips == 1 else 's'} of {p.title.lower()} again"
                + (f", and their {also} with them" if also else "") + ". That "
                "runs after anything missing; until it's done, results mix the old settings and the new."
                + (f" {p.redo_note}" if p.redo_note else "")
                + (" Its stopped redo starts again." if stopped else "")
                + (" It's paused: none of it is made until it's resumed." if held else ""),
                {"artifact": artifact, "clips": clips, "redo_stopped": stopped, "paused": held,
                 "artifacts": [{"artifact": artifact, "title": p.title, "clips": clips}]},
            )
        lineage.resume(session, [artifact], lineage.REDO)
    if uses != used and p.regroup:
        from src.producers import load

        load(p.regroup)(session)
    session.commit()
    # Settings unchanged leave a stopped redo stopped.
    return _with_state(_item(artifact, after, waits, uses), lineage.pauses(session), admin=True)


def _project_clips(request: Request, session: Session, user_id: str, project_id: str) -> list[str]:
    """Every clip in a project the caller can see; 404 otherwise."""
    from src.server.api.routers.projects import _all_project_assets, _can_view
    from src.server.repository.tenant import ProjectRepository

    project = ProjectRepository(session).get_by_id(project_id)
    if project is None or getattr(project, "deleted_at", None) is not None or not _can_view(project, user_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return [a.asset_id for a in _all_project_assets(project, request, session, user_id)]


SWITCH_TITLES = {PAUSE_SCANS: "Scans", PAUSE_UPKEEP: "Upkeep"}


class PauseIn(BaseModel):
    # "work": all of the switch's work (nothing of it starts, missing or
    # stale); "redo": a producer's redo alone (its stale clips wait; what's
    # missing is still made). Named, never inferred.
    scope: Literal["work", "redo"]


def _targets(target: str, scope: str) -> list[str]:
    """What a pause names: one switch (Scans, Upkeep or a producer the
    scheduler makes) or every one ("all", named, never a missing target).
    404 for no such producer; 409 not_scheduled for what scans make (Scans
    pauses it). A redo is a producer's: 422 bad_scope for Scans and Upkeep,
    409 cant_redo for a producer that isn't made again yet; "all" names
    every producer that is."""
    from src.server.api.errors import InvalidChoiceError

    if target == PAUSE_ALL:
        if scope == lineage.WORK:
            return list(pause_targets())
        return [a for a in pause_targets() if a in PRODUCERS and lineage.redoable(a)]
    if target in SWITCH_TITLES:
        if scope == lineage.REDO:
            raise InvalidChoiceError("bad_scope", f"{SWITCH_TITLES[target]} make nothing again: pause its work.",
                                     {"target": target})
        return [target]
    p = producer_or_404(target)
    if scope == lineage.REDO and not lineage.redoable(target):
        raise ConflictError("cant_redo", f"{p.title} isn't made again yet. {lineage.CANT_REDO.get(target, '')}".strip())
    if not p.scheduled:
        raise ConflictError("not_scheduled", f"{p.title} are made by scans: pausing Scans stops them.",
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
    work: scans (no new or changed files found, no thumbnails or video
    previews made), upkeep (no trash purge, file cleanup or face names
    spread; search sync goes on) or a producer (nothing more of it starts,
    missing or stale); all is every switch (state paused; each can be
    resumed alone after; "all" is stored, so a producer a later version
    adds is paused too). Scope redo: a producer's
    stale clips stay as they are until it's resumed, or its settings change
    (what's missing is still made). What's running finishes. Again is
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
    artifact: Literal[PRODUCER_ARTIFACTS]  # type: ignore[valid-type]
    error: str = Field(max_length=10_000)


class FailuresIn(BaseModel):
    items: list[Failure] = Field(max_length=500)


@router.post("/failures", dependencies=[Depends(require_tenant_admin)])  # the scheduler's
def report_failures(body: FailuresIn, session: Annotated[Session, Depends(get_tenant_session)]) -> dict:
    """The scheduler couldn't make these: each is kept with its error and not
    handed out again for 5 minutes, then 10, 20 and so on up to a day, and
    given up after 10 tries until someone asks for it to be tried again. An
    artifact made earlier stays what it was. Clips that don't exist are
    left out. Returns {"recorded"}."""
    ids = list({f.asset_id for f in body.items})
    known = {r[0] for r in session.execute(
        text("SELECT asset_id FROM assets WHERE asset_id = ANY(:ids)"), {"ids": ids})} if ids else set()
    recorded = 0
    for f in body.items:
        if f.asset_id in known:
            lineage.record_failure(session, f.asset_id, f.artifact, f.error or "failed", commit=False)
            recorded += 1
    session.commit()
    return {"recorded": recorded}


class PauseSwitch(BaseModel):
    target: str  # "scans", "upkeep" or a producer's artifact
    title: str
    paused: bool = False
    paused_by: str | None = None  # admins only
    paused_at: datetime | None = None


class StorageSeen(BaseModel):
    """What the scheduler's looks at one library's storage found."""

    seen_at: datetime | None = None  # last seen reachable
    away_since: datetime | None = None  # unreachable since (None: the latest look reached it)
    checked: bool = False  # looked at since the scheduler started


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
    # The account's pause state, derived from its switches (Robert, Oct 9):
    # "running" (none paused: green), "partly" (some: yellow), "paused" (all: red).
    state: Literal["running", "partly", "paused"] = "running"
    # Every pause switch: Scans, Upkeep, then each producer the scheduler makes.
    switches: list[PauseSwitch] = Field(default_factory=list)
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
    import json
    from datetime import timedelta

    from src.server.scheduler.eta import eta
    from src.shared.utils import utcnow

    raw = session.execute(text("SELECT value FROM system_metadata WHERE key = 'scheduler.status'")).scalar()
    status = SchedulerStatus()
    if raw:
        try:
            status = SchedulerStatus(**json.loads(raw))
        except (ValueError, TypeError):
            pass
    now = utcnow()
    status.live = status.at is not None and now - status.at < timedelta(seconds=30)
    held, admin = lineage.pauses(session).work, _admin(request)
    status.state = pause_state(set(held))
    status.switches = [
        PauseSwitch(target=t, title=SWITCH_TITLES.get(t) or PRODUCERS[t].title, paused=t in held,
                    paused_at=held[t]["paused_at"] if t in held else None,
                    paused_by=held[t]["paused_by"] if t in held and admin else None)
        for t in pause_targets()]
    # Paused work has no time left (Robert, Oct 9); what's running still says its time left, since it finishes.
    if status.live:
        held_producers = {t for t in held if t in PRODUCERS}
        try:
            status.eta = Eta(**eta(status.model_dump(), _work_left(request, session, status.storage_waits), now=now,
                                   paused=held_producers))
        except Exception:  # noqa: BLE001 — the time left is extra; what's running is still said
            logger.exception("producers: working out how long is left failed")
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
def list_failures(
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
    rows = lineage.failures(session, artifact=artifact, library_id=library_id, limit=limit + 1, after=after)
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


@router.post("/failures/retry", dependencies=[Depends(require_editor)])
def retry_failures(body: RetryIn, session: Annotated[Session, Depends(get_tenant_session)]) -> dict:
    """Try failing clips again now, given up or not; the back-off starts
    over. asset_ids, or all: true; neither or both is a 400 scope_required.
    Returns {"retried"}."""
    from src.server.api.limits import require_scope

    require_scope(body.asset_ids is not None, body.all, "asset_ids")
    if body.artifact is not None:
        producer_or_404(body.artifact)
    n = lineage.retry(session, artifact=body.artifact, library_id=body.library_id, asset_ids=body.asset_ids)
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
def run_now(body: RunIn, request: Request, session: Annotated[Session, Depends(get_tenant_session)]) -> RunOut:
    """Ask the scheduler to do work now (what lumiverb enrich does): failing
    clips of the producers named are tried again at once and the scheduler
    looks again now; with scope redo (admins), what they've made in the
    libraries named is made again, after anything missing (a person's never
    is). It goes as the scheduler goes: by tier, within its pools, and what's
    paused waits (each producer says). library_ids, or all: true; neither
    or both is a 400 scope_required."""
    from src.server.api.limits import require_scope
    from src.server.repository.tenant import LibraryRepository

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
    for artifact in artifacts:
        want = lineage.desired(session, artifact, models)
        retried = missing = stale = 0
        for library_id in libraries:
            retried += lineage.retry(session, artifact=artifact, library_id=library_id)
            if redo:
                lineage.ask_redo(session, artifact, library_id)
            c = lineage.counts(session, artifact, want, library_id)
            missing, stale = missing + c["missing"], stale + c["stale"]
        out.append(RunProducer(artifact=artifact, title=PRODUCERS[artifact].title, missing=missing,
                               redo=stale if lineage.redoable(artifact) else 0, retried=retried,
                               paused=artifact in paused.work, redo_stopped=artifact in paused.redo))
    lineage.nudge(session)
    session.commit()
    return RunOut(producers=out)


def producer_or_404(artifact: str):
    if artifact not in PRODUCERS:
        raise HTTPException(status_code=404, detail="No such artifact")
    return PRODUCERS[artifact]
