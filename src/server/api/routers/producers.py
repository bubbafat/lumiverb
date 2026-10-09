"""Producers API (ADR-016 phases 3 and 4): each producer, the settings it
makes its artifact with now, and how many clips' artifacts are current,
stale, missing or failing. The scheduler reads the settings here and makes
artifacts with them, so what it records in lineage is what's current.

Redo on change (Robert, Oct 9): changing a setting is the approval, so a
producer's stale clips are made again after anything missing, everywhere.
An admin can stop that for a producer and resume it."""

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
from src.shared.producers import PRODUCERS

PRODUCER_ARTIFACTS = tuple(PRODUCERS)

router = APIRouter(prefix="/v1/producers", tags=["producers"])


class LineageIn(BaseModel):
    """How an artifact a write carries was made: the producer and version that
    made it, the hash of the settings it used (as GET /v1/producers gave
    them), and the SHA-256 of the source file it was made from. A write
    without it is recorded as an unknown producer's, so it isn't current."""

    producer: str = Field(max_length=100)
    version: str = Field(max_length=50)
    settings_hash: str = Field(max_length=64)
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


class ProducerCounts(BaseModel):
    applicable: int  # clips it applies to
    current: int
    stale: int  # made with another producer, version, settings or source
    missing: int  # not made yet (or only failed so far)
    failing: int  # the last try failed (whether or not an older artifact exists)
    given_up: int = 0  # failing, and no longer tried (after 10 tries) until someone asks


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
    counts: ProducerCounts | None = None
    # Whether its stale artifacts are made again (after anything missing),
    # and if they can't be yet, why.
    redoable: bool = True
    why_not: str | None = None
    # An admin stopped its redo: stale clips wait until it's resumed (or its settings change).
    paused: bool = False
    paused_by: str | None = None  # admins only
    paused_at: datetime | None = None


class ProducerList(BaseModel):
    producers: list[ProducerItem]


def tenant_job_models(request: Request) -> dict[str, str]:
    """The tenant's model per AI job (Settings → AI), from the control plane."""
    from src.server.database import get_control_session
    from src.server.repository.ai_machines import job_models
    from src.server.repository.control_plane import TenantRepository

    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        return {}
    with get_control_session() as ctrl:
        tenant = TenantRepository(ctrl).get_by_id(tenant_id)
    return job_models(tenant)


@router.get("", response_model=ProducerList, dependencies=[Depends(require_signed_in)])
def list_producers(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    library_id: str | None = None,
    project_id: str | None = None,
    counts: bool = True,
) -> ProducerList:
    """Every producer with its settings now and whether its redo is stopped;
    counts (in one library or project when given) unless counts=false, as the scheduler asks."""
    models = tenant_job_models(request)
    asset_ids = None
    if counts and project_id:
        require_editor(request)  # like every project route
        asset_ids = _project_clips(request, session, user_id, project_id)
    admin = getattr(request.state, "role", None) == "admin"
    stopped = lineage.paused(session)
    items = []
    for artifact, p in PRODUCERS.items():
        want = lineage.desired(session, artifact, models)
        item = ProducerItem(
            artifact=artifact, producer=p.producer, version=p.version, title=p.title, media=list(p.media),
            uniform=p.uniform, settings=want["settings"], settings_hash=want["settings_hash"],
            redoable=lineage.redoable(artifact), why_not=lineage.CANT_REDO.get(artifact),
        )
        if artifact in stopped:
            item.paused = True
            item.paused_at = stopped[artifact]["paused_at"]
            item.paused_by = stopped[artifact]["paused_by"] if admin else None
        if counts:
            item.counts = ProducerCounts(**lineage.counts(session, artifact, want, library_id, asset_ids))
        items.append(item)
    return ProducerList(producers=items)


def _project_clips(request: Request, session: Session, user_id: str, project_id: str) -> list[str]:
    """Every clip in a project the caller can see (a smart project's live results too); 404 otherwise."""
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
