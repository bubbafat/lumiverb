"""Jobs API: enqueue, claim, complete, fail. All require tenant auth."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.core.config import get_settings
from src.models.filter import AssetFilterSpec
from src.repository.tenant import (
    AssetRepository,
    AssetMetadataRepository,
    LibraryRepository,
    SearchSyncQueueRepository,
    WorkerJobRepository,
)
from src.workers.enqueue import enqueue_jobs_for_filter
from src.workers.vision import VISION_MODEL_ID, VISION_MODEL_VERSION

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


class EnqueueRequest(BaseModel):
    job_type: str
    filter: AssetFilterSpec
    force: bool = False


class EnqueueResponse(BaseModel):
    enqueued: int


@router.post("/enqueue", response_model=EnqueueResponse)
def enqueue_jobs(
    body: EnqueueRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> EnqueueResponse:
    """
    Enqueue jobs for assets matching filter spec.
    force=True cancels existing pending/claimed jobs and re-enqueues.
    """
    n = enqueue_jobs_for_filter(
        session=session,
        filter=body.filter,
        job_type=body.job_type,
        force=body.force,
    )
    return EnqueueResponse(enqueued=n)


# ---------------------------------------------------------------------------
# Claim next job (workers)
# ---------------------------------------------------------------------------


@router.get("/next", response_model=None)
def get_next_job(
    job_type: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str | None = None,
) -> Response | dict[str, Any]:
    """
    Claim next pending job of type. Returns 204 if none available.
    Returns job payload with asset and library info for processing.
    """
    settings = get_settings()
    worker_id = f"api_{uuid.uuid4().hex[:12]}"
    job_repo = WorkerJobRepository(session)
    job = job_repo.claim_next(
        job_type=job_type,
        worker_id=worker_id,
        lease_minutes=settings.worker_lease_minutes,
        library_id=library_id,
    )
    if job is None:
        return Response(status_code=204)

    asset_repo = AssetRepository(session)
    library_repo = LibraryRepository(session)
    asset = asset_repo.get_by_id(job.asset_id) if job.asset_id else None
    if asset is None:
        job_repo.set_failed(job, "Asset not found")
        raise HTTPException(status_code=404, detail="Asset not found")
    library = library_repo.get_by_id(asset.library_id)
    if library is None:
        job_repo.set_failed(job, "Library not found")
        raise HTTPException(status_code=404, detail="Library not found")

    return {
        "job_id": job.job_id,
        "job_type": job.job_type,
        "asset_id": asset.asset_id,
        "rel_path": asset.rel_path,
        "media_type": asset.media_type,
        "library_id": asset.library_id,
        "root_path": library.root_path,
        "proxy_key": asset.proxy_key,
        "thumbnail_key": asset.thumbnail_key,
    }


# ---------------------------------------------------------------------------
# Complete / fail (workers)
# ---------------------------------------------------------------------------


class JobCompleteResponse(BaseModel):
    job_id: str
    status: str


class JobCompleteBody(BaseModel):
    model_config = {"extra": "allow"}


class JobFailBody(BaseModel):
    error_message: str


class JobFailResponse(BaseModel):
    job_id: str
    status: str


@router.post("/{job_id}/complete", response_model=JobCompleteResponse)
def complete_job(
    job_id: str,
    body: JobCompleteBody,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> JobCompleteResponse:
    job_repo = WorkerJobRepository(session)
    job = job_repo.get_by_id(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "claimed":
        raise HTTPException(
            status_code=409,
            detail=f"Job not claimed (status={job.status})",
        )

    data = body.model_dump()
    if job.job_type == "proxy":
        proxy_key = data.get("proxy_key")
        thumbnail_key = data.get("thumbnail_key")
        width = data.get("width")
        height = data.get("height")
        if proxy_key is None or thumbnail_key is None or width is None or height is None:
            raise HTTPException(
                status_code=400,
                detail="proxy_key, thumbnail_key, width, height required for proxy jobs",
            )
        if job.asset_id is None:
            raise HTTPException(status_code=400, detail="Job has no asset_id")
        asset_repo = AssetRepository(session)
        asset_repo.update_proxy(
            job.asset_id,
            proxy_key=proxy_key,
            thumbnail_key=thumbnail_key,
            width=width,
            height=height,
        )
    elif job.job_type == "exif":
        if job.asset_id is None:
            raise HTTPException(status_code=400, detail="Job has no asset_id")
        asset_repo = AssetRepository(session)
        asset_repo.update_exif(
            asset_id=job.asset_id,
            sha256=data.get("sha256"),
            exif=data.get("exif", {}),
            camera_make=data.get("camera_make"),
            camera_model=data.get("camera_model"),
            taken_at=data.get("taken_at"),
            gps_lat=data.get("gps_lat"),
            gps_lon=data.get("gps_lon"),
        )
    elif job.job_type == "ai_vision":
        if job.asset_id is None:
            raise HTTPException(status_code=400, detail="Job has no asset_id")
        model_id = data.get("model_id", VISION_MODEL_ID)
        model_version = data.get("model_version", VISION_MODEL_VERSION)
        description = data.get("description", "")
        tags = data.get("tags", [])
        meta_repo = AssetMetadataRepository(session)
        meta_repo.upsert(
            asset_id=job.asset_id,
            model_id=model_id,
            model_version=model_version,
            data={
                "description": description,
                "tags": tags,
            },
        )
        # Enqueue search sync so Quickwit can be updated for this asset.
        queue_repo = SearchSyncQueueRepository(session)
        queue_repo.enqueue(asset_id=job.asset_id, operation="upsert")
    job_repo.set_completed(job)
    return JobCompleteResponse(job_id=job_id, status="completed")


@router.post("/{job_id}/fail", response_model=JobFailResponse)
def fail_job(
    job_id: str,
    body: JobFailBody,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> JobFailResponse:
    job_repo = WorkerJobRepository(session)
    job = job_repo.get_by_id(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "claimed":
        raise HTTPException(
            status_code=409,
            detail=f"Job not claimed (status={job.status})",
        )
    job_repo.set_failed(job, body.error_message)
    return JobFailResponse(job_id=job_id, status="failed")


# ---------------------------------------------------------------------------
# List jobs (for tests / debugging)
# ---------------------------------------------------------------------------


class JobListItem(BaseModel):
    job_id: str
    job_type: str
    asset_id: str | None
    status: str


@router.get("", response_model=list[JobListItem])
def list_jobs(
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str | None = None,
) -> list[JobListItem]:
    """List jobs, optionally filtered by library_id (via asset)."""
    from sqlmodel import select
    from src.models.tenant import Asset, WorkerJob
    if library_id:
        stmt = (
            select(WorkerJob)
            .join(Asset, WorkerJob.asset_id == Asset.asset_id)
            .where(Asset.library_id == library_id)
        )
    else:
        stmt = select(WorkerJob)
    jobs = list(session.exec(stmt).all())
    return [
        JobListItem(
            job_id=j.job_id,
            job_type=j.job_type,
            asset_id=j.asset_id,
            status=j.status,
        )
        for j in jobs
    ]


# ---------------------------------------------------------------------------
# Job status (for tests / debugging)
# ---------------------------------------------------------------------------


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    error_message: str | None


@router.get("/{job_id}/status", response_model=JobStatusResponse)
def get_job_status(
    job_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> JobStatusResponse:
    job_repo = WorkerJobRepository(session)
    job = job_repo.get_by_id(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        error_message=job.error_message,
    )
