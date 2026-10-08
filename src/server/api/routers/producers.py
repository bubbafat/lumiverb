"""Producers API (ADR-016 phase 3): each producer, the settings it makes its
artifact with now, and how many clips' artifacts are current, stale, missing
or failing. The worker reads the settings here and makes artifacts with
them, so what it records in lineage is what's current."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlmodel import Session

from src.server.api.dependencies import get_tenant_session, require_editor, require_signed_in, require_tenant_admin
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


def lineage_dict(value: "LineageIn | dict | str | None", source_sha256: str | None = None) -> dict | None:
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


class ProducerList(BaseModel):
    producers: list[ProducerItem]


def tenant_vision_model(request: Request) -> str:
    """The tenant's vision model from the control plane, if any."""
    from src.server.database import get_control_session
    from src.server.repository.control_plane import TenantRepository

    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        return ""
    with get_control_session() as ctrl:
        tenant = TenantRepository(ctrl).get_by_id(tenant_id)
    return (tenant.vision_model_id if tenant else "") or ""


@router.get("", response_model=ProducerList, dependencies=[Depends(require_signed_in)])
def list_producers(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str | None = None,
    counts: bool = True,
) -> ProducerList:
    """Every producer with its settings now; counts (in one library when given)
    unless counts=false, as the worker asks."""
    vision_model = tenant_vision_model(request)
    items = []
    for artifact, p in PRODUCERS.items():
        want = lineage.desired(session, artifact, vision_model)
        items.append(ProducerItem(
            artifact=artifact, producer=p.producer, version=p.version, title=p.title, media=list(p.media),
            uniform=p.uniform, settings=want["settings"], settings_hash=want["settings_hash"],
            counts=ProducerCounts(**lineage.counts(session, artifact, want, library_id)) if counts else None,
        ))
    return ProducerList(producers=items)


class AdoptModel(BaseModel):
    model: str = Field(min_length=1, max_length=200)


@router.post("/vision-model", response_model=ProducerList, dependencies=[Depends(require_tenant_admin)])
def adopt_vision_model(
    body: AdoptModel,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> ProducerList:
    """Record the vision model the worker is configured with, when the account
    has none yet: descriptions, OCR and scene descriptions are then current
    against it. Once set, changing it is a setting change (it makes them
    stale), not this. 409 if one is set already."""
    from src.server.api.errors import ConflictError

    current = lineage.account_settings(session, tenant_vision_model(request))["model"]
    if current and current != body.model:
        raise ConflictError("vision_model_set", f"The account's vision model is {current}.", {"model": current})
    if not current:
        session.execute(text(
            "INSERT INTO system_metadata (key, value, updated_at) VALUES (:k, :v, now())"
            " ON CONFLICT (key) DO NOTHING"
        ), {"k": lineage.ACCOUNT_VISION_MODEL, "v": body.model})
        session.commit()
    return list_producers(request, session, counts=False)


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
