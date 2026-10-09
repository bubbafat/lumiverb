"""Producers API (ADR-016 phases 3 and 4): each producer, the settings it
makes its artifact with now, and how many clips' artifacts are current,
stale, missing or failing. The scheduler reads the settings here and makes
artifacts with them, so what it records in lineage is what's current.

Redo on change (Robert, Oct 9): changing a setting is the approval, so a
producer's stale clips are made again after anything missing, everywhere.
An admin can stop that for a producer and resume it.

Pausing (Robert, Oct 9): an admin pauses all of the account's processing,
or one producer's, and resumes it. The scheduler starts nothing more of
it; what's running finishes."""

from __future__ import annotations

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
from src.server.repository import lineage
from src.shared.producers import PAUSE_ALL, PRODUCERS

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
    kind: str  # "int" | "float" | "text"
    value: Any
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    advanced: bool = False
    fixed: str | None = None


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
    stopped, held = lineage.paused(session), lineage.processing_paused(session)
    items = []
    for artifact in PRODUCERS:
        want = lineage.desired(session, artifact, models)
        item = _with_state(_item(artifact, want, waits), stopped, held, admin)
        if counts:
            item.counts = ProducerCounts(**lineage.counts(session, artifact, want, library_id, asset_ids))
        items.append(item)
    return ProducerList(producers=items)


def _admin(request: Request) -> bool:
    return getattr(request.state, "role", None) == "admin"


def _with_state(item: ProducerItem, stopped: dict, held: dict, admin: bool) -> ProducerItem:
    """Whether its redo is stopped and whether it's paused (who did it: admins only)."""
    if (redo := stopped.get(item.artifact)) is not None:
        item.redo_stopped, item.redo_stopped_at = True, redo["paused_at"]
        item.redo_stopped_by = redo["paused_by"] if admin else None
    if (pause := held.get(item.artifact)) is not None:
        item.paused, item.paused_at = True, pause["paused_at"]
        item.paused_by = pause["paused_by"] if admin else None
    return item


def _item(artifact: str, want: dict[str, Any], waits: dict[str, str | None]) -> ProducerItem:
    p = PRODUCERS[artifact]
    return ProducerItem(
        artifact=artifact, producer=p.producer, version=p.version, title=p.title, media=list(p.media),
        uniform=p.uniform, settings=want["settings"], settings_hash=want["settings_hash"],
        fields=[SettingField(key=s.key, label=s.label, kind=s.kind, value=want["settings"].get(s.key, s.default),
                             default=s.default, minimum=s.minimum, maximum=s.maximum, unit=s.unit,
                             advanced=s.advanced, fixed=s.fixed or None)
                for s in p.settings],
        redoable=lineage.redoable(artifact), why_not=lineage.CANT_REDO.get(artifact),
        waiting=waits.get(p.job) if p.job else None,
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
    a pause stays."""
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
    before = lineage.desired(session, artifact, models)
    lineage.set_overrides(session, artifact, values)
    after = lineage.desired(session, artifact, models)
    if after["settings_hash"] != before["settings_hash"]:
        clips = lineage.would_redo(session, artifact, after)
        if clips and not body.redo:
            stopped = artifact in lineage.paused(session)
            held = artifact in lineage.processing_paused(session)
            session.rollback()
            also = " and ".join(PRODUCERS[a].title.lower() for a in p.redo_also)
            raise DecisionRequiredError(
                "redo_on_change",
                f"New settings make {clips:,} clip{'' if clips == 1 else 's'} of {p.title.lower()} again"
                + (f", and their {also} with them" if also else "") + ". That "
                "runs after anything missing; until it's done, results mix the old settings and the new."
                + (" Its stopped redo starts again." if stopped else "")
                + (" It's paused: none of it is made until it's resumed." if held else ""),
                {"artifact": artifact, "clips": clips, "redo_stopped": stopped, "paused": held,
                 "artifacts": [{"artifact": artifact, "title": p.title, "clips": clips}]},
            )
        lineage.resume(session, [artifact])
    session.commit()
    # Settings unchanged leave a stopped redo stopped.
    return _with_state(_item(artifact, after, waits), lineage.paused(session), lineage.processing_paused(session),
                       admin=True)


def _project_clips(request: Request, session: Session, user_id: str, project_id: str) -> list[str]:
    """Every clip in a project the caller can see; 404 otherwise."""
    from src.server.api.routers.projects import _all_project_assets, _can_view
    from src.server.repository.tenant import ProjectRepository

    project = ProjectRepository(session).get_by_id(project_id)
    if project is None or getattr(project, "deleted_at", None) is not None or not _can_view(project, user_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return [a.asset_id for a in _all_project_assets(project, request, session, user_id)]


@router.post("/{artifact}/redo/stop", status_code=204, dependencies=[Depends(require_tenant_admin)])
def stop_redo(
    artifact: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> None:
    """Stop redoing the producer's stale clips: they stay as they are until
    it's resumed, or its settings change. What's missing is still made."""
    p = producer_or_404(artifact)
    if not lineage.redoable(artifact):
        raise ConflictError("cant_redo", f"{p.title} isn't made again yet. {lineage.CANT_REDO.get(artifact, '')}".strip())
    lineage.pause(session, artifact, by=user_id)
    session.commit()


@router.post("/{artifact}/redo/resume", status_code=204, dependencies=[Depends(require_tenant_admin)])
def resume_redo(artifact: str, session: Annotated[Session, Depends(get_tenant_session)]) -> None:
    """Redo the producer's stale clips again, after anything missing."""
    producer_or_404(artifact)
    lineage.resume(session, [artifact])
    session.commit()


@router.post("/pause", status_code=204, dependencies=[Depends(require_tenant_admin)])
def pause_all(
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> None:
    """Pause all of the account's processing (admins): the scheduler starts
    nothing more, scans included, until it's resumed; what's running
    finishes. Again is fine: who paused it first, and when, stay."""
    lineage.pause_processing(session, PAUSE_ALL, by=user_id)
    session.commit()


@router.post("/resume", status_code=204, dependencies=[Depends(require_tenant_admin)])
def resume_all(session: Annotated[Session, Depends(get_tenant_session)]) -> None:
    """Carry on with all of the account's processing (admins). Producers
    paused one by one stay paused."""
    lineage.resume_processing(session, PAUSE_ALL)
    session.commit()


@router.post("/{artifact}/pause", status_code=204, dependencies=[Depends(require_tenant_admin)])
def pause_producer(
    artifact: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> None:
    """Pause one producer (admins): nothing more of it starts, missing or
    stale, until it's resumed; what's running finishes. 409 not_scheduled
    for what scans make (proxies, video previews): pausing all processing
    stops those."""
    p = producer_or_404(artifact)
    if not p.scheduled:
        raise ConflictError("not_scheduled", f"{p.title} are made by scans, not on their own: pausing all "
                                             "processing stops them.", {"artifact": artifact})
    lineage.pause_processing(session, artifact, by=user_id)
    session.commit()


@router.post("/{artifact}/resume", status_code=204, dependencies=[Depends(require_tenant_admin)])
def resume_producer(artifact: str, session: Annotated[Session, Depends(get_tenant_session)]) -> None:
    """Carry on with a paused producer (admins)."""
    producer_or_404(artifact)
    lineage.resume_processing(session, artifact)
    session.commit()


class Failure(BaseModel):
    asset_id: str = Field(min_length=1, max_length=64)
    artifact: Literal[PRODUCER_ARTIFACTS]  # type: ignore[valid-type]
    error: str = Field(max_length=10_000)


class FailuresIn(BaseModel):
    items: list[Failure] = Field(max_length=500)


@router.post("/failures", dependencies=[Depends(require_editor)])
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
    # An admin paused all of the account's processing: nothing more starts
    # until it's resumed (POST /v1/producers/pause, /resume).
    paused: bool = False
    paused_by: str | None = None  # admins only
    paused_at: datetime | None = None


@router.get("/queue", response_model=SchedulerStatus, dependencies=[Depends(require_signed_in)])
def scheduler_status(request: Request, session: Annotated[Session, Depends(get_tenant_session)]) -> SchedulerStatus:
    """What the scheduler is doing now, and whether an admin paused it all;
    live is false when it hasn't said for 30 seconds."""
    import json
    from datetime import timedelta

    from src.shared.utils import utcnow

    raw = session.execute(text("SELECT value FROM system_metadata WHERE key = 'scheduler.status'")).scalar()
    status = SchedulerStatus()
    if raw:
        try:
            status = SchedulerStatus(**json.loads(raw))
        except (ValueError, TypeError):
            pass
    status.live = status.at is not None and utcnow() - status.at < timedelta(seconds=30)
    if (pause := lineage.processing_paused(session).get(PAUSE_ALL)) is not None:
        status.paused, status.paused_at = True, pause["paused_at"]
        status.paused_by = pause["paused_by"] if _admin(request) else None
    return status


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
    """Which failing clips to try again: one producer's, one library's, or
    these clips' (any combination; nothing given = every failing clip)."""

    artifact: str | None = Field(default=None, max_length=64)
    library_id: str | None = Field(default=None, max_length=64)
    asset_ids: list[str] | None = Field(default=None, max_length=10_000)


@router.post("/failures/retry", dependencies=[Depends(require_editor)])
def retry_failures(body: RetryIn, session: Annotated[Session, Depends(get_tenant_session)]) -> dict:
    """Try failing clips again now, given up or not; the back-off starts
    over. Returns {"retried"}."""
    if body.artifact is not None:
        producer_or_404(body.artifact)
    n = lineage.retry(session, artifact=body.artifact, library_id=body.library_id, asset_ids=body.asset_ids)
    session.commit()
    return {"retried": n}


def producer_or_404(artifact: str):
    if artifact not in PRODUCERS:
        raise HTTPException(status_code=404, detail="No such artifact")
    return PRODUCERS[artifact]
