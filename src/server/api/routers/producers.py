"""Producers API (ADR-016 phase 3): each producer, the settings it makes its
artifact with now, and how many clips' artifacts are current, stale, missing
or failing. The worker reads the settings here and makes artifacts with
them, so what it records in lineage is what's current.

Upgrades (piece 5): an admin approves making a producer's stale artifacts
again, in everything, one library or one project. The API asks first, with
a 409 the request answers: what to do with clips a person edited
(edited_clips), and for producers that must stay one model across the
library, a confirmation naming the count (redo_everything)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
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
from src.server.api.errors import ConflictError, DecisionRequiredError, InvalidChoiceError
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


class UpgradeScope(BaseModel):
    kind: Literal["all", "library", "project"]
    id: str | None = None  # the library's or project's
    name: str | None = None


class Upgrade(BaseModel):
    """Stale artifacts an admin approved making again: the clips stale in its
    scope then, each until it's made again."""

    upgrade_id: str
    scope: UpgradeScope
    edits: Literal["keep", "replace", "skip"]
    approved_by: str | None = None
    approved_at: datetime
    total: int  # clips it took on (still in sight)
    remaining: int  # not made again yet
    # Made again since, but still not what it upgrades to (e.g. a worker that
    # read the settings before they changed): stale, and not handed out again.
    still_stale: int = 0


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
    # Whether its stale artifacts can be made again now, and if not why.
    upgradable: bool = True
    why_not: str | None = None
    # Stale clips (in the counts' scope) with a person's edits on top.
    edited: int = 0
    upgrades: list[Upgrade] = Field(default_factory=list)


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
    """Every producer with its settings now and its upgrades; counts (in one
    library or project when given) unless counts=false, as the worker asks."""
    models = tenant_job_models(request)
    asset_ids = None
    if counts and project_id:
        require_editor(request)  # like every project route
        asset_ids = _project_clips(request, session, user_id, project_id)
    if counts:
        lineage.retire_outdated(session, models)
    admin = getattr(request.state, "role", None) == "admin"
    items = []
    for artifact, p in PRODUCERS.items():
        want = lineage.desired(session, artifact, models)
        item = ProducerItem(
            artifact=artifact, producer=p.producer, version=p.version, title=p.title, media=list(p.media),
            uniform=p.uniform, settings=want["settings"], settings_hash=want["settings_hash"],
            upgradable=artifact not in lineage.CANT_UPGRADE, why_not=lineage.CANT_UPGRADE.get(artifact),
        )
        if counts:
            item.counts = ProducerCounts(**lineage.counts(session, artifact, want, library_id, asset_ids))
            if item.counts.stale:
                item.edited = lineage.edited_stale_count(session, artifact, want, library_id, asset_ids)
            item.upgrades = [Upgrade(**{**u, "approved_by": u["approved_by"] if admin else None})
                             for u in lineage.upgrades(session, artifact)]
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


class UpgradeIn(BaseModel):
    """Make the producer's stale artifacts again: everywhere, or in one
    library or project. edits answers 409 edited_clips; confirm answers 409
    redo_everything."""

    library_id: str | None = Field(default=None, max_length=64)
    project_id: str | None = Field(default=None, max_length=64)
    edits: Literal["keep", "replace", "skip"] | None = None
    confirm: bool = False


class UpgradeOut(BaseModel):
    upgrade_id: str | None  # None when every stale clip was skipped
    artifact: str
    scope: UpgradeScope
    edits: Literal["keep", "replace", "skip"]
    upgrading: int  # clips handed to the worker to make again
    skipped_edited: int = 0  # left as they are: a person edited them
    # edits=replace: clips whose edits move to history as each is made again.
    edits_to_replace: int = 0


@router.post("/{artifact}/upgrade", response_model=UpgradeOut, dependencies=[Depends(require_tenant_admin)])
def approve_upgrade(
    artifact: str,
    body: UpgradeIn,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> UpgradeOut:
    """Approve making the producer's stale artifacts again, in scope. Takes
    the clips stale there now; the worker makes them after anything missing.
    Approving the same scope again replaces the earlier approval. With
    edits=replace, a clip's edits move to history when it's made again."""
    p = producer_or_404(artifact)
    if body.library_id and body.project_id:
        raise InvalidChoiceError("one_scope", "Narrow an upgrade to a library or a project, not both.")
    if artifact in lineage.CANT_UPGRADE:
        raise ConflictError("cant_upgrade", f"{p.title} can't be made again yet. {lineage.CANT_UPGRADE[artifact]}")
    narrowed = body.library_id or body.project_id
    if p.uniform and narrowed:
        raise InvalidChoiceError(
            "all_or_nothing",
            f"{p.title} must come from one model across the library, so it's upgraded everywhere or not at all.")

    scope = UpgradeScope(kind="all")
    asset_ids: list[str] | None = None
    if body.library_id:
        from src.server.repository.tenant import LibraryRepository

        lib = LibraryRepository(session).get_by_id(body.library_id)
        if lib is None or getattr(lib, "status", "active") == "trashed":
            raise HTTPException(status_code=404, detail="Library not found")
        scope = UpgradeScope(kind="library", id=lib.library_id, name=lib.name)
    elif body.project_id:
        asset_ids = _project_clips(request, session, user_id, body.project_id)
        from src.server.repository.tenant import ProjectRepository

        scope = UpgradeScope(kind="project", id=body.project_id,
                             name=ProjectRepository(session).get_by_id(body.project_id).name)

    models = tenant_job_models(request)
    lineage.retire_outdated(session, models)
    want = lineage.desired(session, artifact, models)
    stale, edited = lineage.stale_ids(session, artifact, want, body.library_id, asset_ids)
    where = {"all": "", "library": f" in {scope.name}", "project": f" in {scope.name}"}[scope.kind]
    if not stale:
        raise ConflictError("nothing_stale", f"Nothing{where} was made with older {p.title.lower()} settings.")
    if p.uniform and not body.confirm:
        whose = "The 1 clip's" if len(stale) == 1 else f"All {len(stale):,} clips'"
        raise DecisionRequiredError(
            "redo_everything",
            f"{whose} {p.title.lower()} will be made again. Until it finishes, results mix the old and the new.",
            {"artifact": artifact, "title": p.title, "stale": len(stale)},
        )
    if edited and body.edits is None:
        raise DecisionRequiredError(
            "edited_clips",
            f"{len(edited):,} of the {len(stale):,} clips have your edits. Keep them on top, replace them "
            "(they're kept in history) or skip those clips?",
            {"artifact": artifact, "stale": len(stale), "edited": len(edited),
             "choices": ["keep", "replace", "skip"]},
        )
    edits = body.edits or "keep"
    items = [a for a in stale if a not in edited] if edits == "skip" else stale
    skipped = len(stale) - len(items)
    from sqlalchemy.exc import IntegrityError

    try:
        # Replaces an earlier approval for the same scope (with nothing, when every clip was skipped).
        upgrade_id = lineage.approve(session, artifact, want, scope=scope.kind, library_id=body.library_id,
                                     project_id=body.project_id, edits=edits, asset_ids=items, by=user_id)
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ConflictError("upgrade_changed", "Someone approved this upgrade at the same moment. Look again.")
    return UpgradeOut(upgrade_id=upgrade_id, artifact=artifact, scope=scope, edits=edits, upgrading=len(items),
                      skipped_edited=skipped, edits_to_replace=len(edited) if edits == "replace" else 0)


@router.delete("/{artifact}/upgrade", status_code=204, dependencies=[Depends(require_tenant_admin)])
def cancel_upgrade(
    artifact: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    upgrade_id: str | None = None,
) -> None:
    """Stop an upgrade (or every upgrade of the producer): what it hasn't made
    again yet stays stale, with its edits. Edits it already replaced stay in history."""
    producer_or_404(artifact)
    r = session.execute(text(
        "DELETE FROM producer_upgrades WHERE artifact = :artifact AND (CAST(:u AS text) IS NULL OR upgrade_id = :u)"
    ), {"artifact": artifact, "u": upgrade_id})
    if upgrade_id and r.rowcount == 0:
        raise HTTPException(status_code=404, detail="No such upgrade")
    session.commit()


class Failure(BaseModel):
    asset_id: str = Field(min_length=1, max_length=64)
    artifact: Literal[PRODUCER_ARTIFACTS]  # type: ignore[valid-type]
    error: str = Field(max_length=10_000)


class FailuresIn(BaseModel):
    items: list[Failure] = Field(max_length=500)


@router.post("/failures", dependencies=[Depends(require_editor)])
def report_failures(body: FailuresIn, session: Annotated[Session, Depends(get_tenant_session)]) -> dict:
    """The worker couldn't make these: each is kept with its error and not
    handed out again for 5 minutes, then 10, 20 and so on up to a day. An
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


def producer_or_404(artifact: str):
    if artifact not in PRODUCERS:
        raise HTTPException(status_code=404, detail="No such artifact")
    return PRODUCERS[artifact]
