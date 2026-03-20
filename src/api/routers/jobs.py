"""Jobs API: enqueue, claim, complete, fail. All require tenant auth."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_db_session, get_tenant_session
from src.api.routers.maintenance import get_maintenance_state
from src.core.config import get_settings
from src.core import asset_status
from src.models.filter import AssetFilterSpec
from src.repository.control_plane import TenantRepository
from src.repository.tenant import (
    AssetRepository,
    AssetEmbeddingRepository,
    AssetMetadataRepository,
    LibraryRepository,
    SearchSyncQueueRepository,
    WorkerJobRepository,
)
from src.workers.enqueue import enqueue_jobs_for_filter

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
    filter.retry_failed=True re-enqueues only assets with failed jobs (mutually exclusive with force).
    """
    if body.force and body.filter.retry_failed:
        raise HTTPException(
            status_code=400,
            detail="force and retry_failed are mutually exclusive",
        )
    n = enqueue_jobs_for_filter(
        session=session,
        asset_filter=body.filter,
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
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    cp_session: Annotated[Session, Depends(get_db_session)],
    library_id: str | None = None,
    path_prefix: str | None = None,
) -> Response | dict[str, Any]:
    """
    Claim next pending job of type. Returns 204 if none available.
    Returns job payload with asset and library info for processing.
    """
    if get_maintenance_state(session).get("active"):
        return Response(status_code=204)

    settings = get_settings()
    worker_id = f"api_{uuid.uuid4().hex[:12]}"
    job_repo = WorkerJobRepository(session)
    job = job_repo.claim_next(
        job_type=job_type,
        worker_id=worker_id,
        lease_minutes=settings.worker_lease_minutes,
        library_id=library_id,
        path_prefix=path_prefix,
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

    tenant_id = getattr(request.state, "tenant_id", None)
    tenant_repo = TenantRepository(cp_session)
    tenant = tenant_repo.get_by_id(tenant_id) if tenant_id else None

    result: dict[str, Any] = {
        "job_id": job.job_id,
        "job_type": job.job_type,
        "asset_id": asset.asset_id,
        "rel_path": asset.rel_path,
        "media_type": asset.media_type,
        "library_id": asset.library_id,
        "root_path": library.root_path,
        "proxy_key": asset.proxy_key,
        "thumbnail_key": asset.thumbnail_key,
        "vision_model_id": library.vision_model_id,
        "vision_api_url": tenant.vision_api_url if tenant else "",
        "vision_api_key": tenant.vision_api_key if tenant else "",
    }
    if asset.duration_sec is not None:
        result["duration_sec"] = asset.duration_sec
    elif asset.duration_ms is not None:
        result["duration_sec"] = asset.duration_ms / 1000.0
    return result


# ---------------------------------------------------------------------------
# Pending count (workers)
# ---------------------------------------------------------------------------


class PendingCountResponse(BaseModel):
    pending: int


@router.get("/pending", response_model=PendingCountResponse)
def get_pending_count(
    job_type: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str | None = None,
    path_prefix: str | None = None,
) -> PendingCountResponse:
    """
    Count pending/claimed jobs of type. Same filters as GET /next.
    Used by workers for progress display (total work remaining).
    """
    job_repo = WorkerJobRepository(session)
    count = job_repo.pending_count(
        job_type=job_type,
        library_id=library_id,
        path_prefix=path_prefix,
    )
    return PendingCountResponse(pending=count)


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
        proxy_sha256 = data.get("proxy_sha256")
        thumbnail_sha256 = data.get("thumbnail_sha256")
        if (
            proxy_key is not None
            and thumbnail_key is not None
            and width is not None
            and height is not None
        ):
            if job.asset_id is None:
                raise HTTPException(status_code=400, detail="Job has no asset_id")
            asset_repo = AssetRepository(session)
            asset_repo.update_proxy(
                job.asset_id,
                proxy_key=proxy_key,
                thumbnail_key=thumbnail_key,
                width=width,
                height=height,
                proxy_sha256=proxy_sha256,
                thumbnail_sha256=thumbnail_sha256,
            )
        # Else: skipped (e.g. video proxy deferred) — just mark job completed, no asset update
    elif job.job_type == "video-preview":
        video_preview_key = data.get("video_preview_key")
        if video_preview_key is None:
            raise HTTPException(
                status_code=400,
                detail="video_preview_key required for video-preview jobs",
            )
        if job.asset_id is None:
            raise HTTPException(status_code=400, detail="Job has no asset_id")
        asset_repo = AssetRepository(session)
        asset_repo.set_video_preview(job.asset_id, video_preview_key=video_preview_key)
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
            duration_sec=data.get("duration_sec"),
        )
    elif job.job_type == "ai_vision":
        if job.asset_id is None:
            raise HTTPException(status_code=400, detail="Job has no asset_id")
        model_id = data.get("model_id", "")
        model_version = data.get("model_version", "1")
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
        # Advance asset status to described.
        asset_repo = AssetRepository(session)
        asset_repo.set_status(job.asset_id, asset_status.DESCRIBED)
    elif job.job_type == "embed":
        if job.asset_id is None:
            raise HTTPException(status_code=400, detail="Job has no asset_id")
        embeddings = data.get("embeddings")
        if not embeddings or not isinstance(embeddings, list):
            raise HTTPException(
                status_code=400,
                detail="embeddings list required for embed jobs",
            )
        emb_repo = AssetEmbeddingRepository(session)
        for item in embeddings:
            model_id = item.get("model_id")
            model_version = item.get("model_version")
            vector = item.get("vector")
            if not model_id or not model_version or not isinstance(vector, list):
                raise HTTPException(
                    status_code=400,
                    detail="Each embedding must have model_id, model_version, and vector",
                )
            emb_repo.upsert(
                asset_id=job.asset_id,
                model_id=model_id,
                model_version=model_version,
                vector=[float(x) for x in vector],
            )
    elif job.job_type == "video-index":
        # Chunk work is done via video API; worker just marks job complete.
        pass
    elif job.job_type == "video-vision":
        if job.asset_id is None:
            raise HTTPException(status_code=400, detail="Job has no asset_id")
        asset_repo = AssetRepository(session)
        asset_repo.set_video_indexed(job.asset_id)
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


@router.post("/{job_id}/block", response_model=JobFailResponse)
def block_job(
    job_id: str,
    body: JobFailBody,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> JobFailResponse:
    """Immediately block a job without incrementing fail_count.
    Used for permanent failures (e.g. wrong media type) that should never be retried."""
    job_repo = WorkerJobRepository(session)
    job = job_repo.get_by_id(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "claimed":
        raise HTTPException(
            status_code=409,
            detail=f"Job not claimed (status={job.status})",
        )
    job_repo.set_blocked(job, body.error_message)
    return JobFailResponse(job_id=job_id, status="blocked")


# ---------------------------------------------------------------------------
# Failures listing
# ---------------------------------------------------------------------------


class FailureRow(BaseModel):
    rel_path: str
    error_message: str
    failed_at: str | None


class FailuresResponse(BaseModel):
    rows: list[FailureRow]
    total_count: int


@router.get("/failures", response_model=FailuresResponse)
def list_failures(
    library_id: str,
    job_type: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    path_prefix: str | None = None,
    limit: int = 20,
) -> FailuresResponse:
    """
    List failed jobs for a library and job type.
    Returns the most recent failed job per asset, ordered by rel_path.
    """
    job_repo = WorkerJobRepository(session)
    rows, total_count = job_repo.list_failures(
        library_id=library_id,
        job_type=job_type,
        path_prefix=path_prefix,
        limit=limit,
    )
    return FailuresResponse(
        rows=[
            FailureRow(
                rel_path=r["rel_path"],
                error_message=r["error_message"] or "",
                failed_at=r["failed_at"].isoformat() if r["failed_at"] else None,
            )
            for r in rows
        ],
        total_count=total_count,
    )


# ---------------------------------------------------------------------------
# Job stats (aggregate counts for admin UI)
# ---------------------------------------------------------------------------


class JobStatRow(BaseModel):
    job_type: str
    pending: int
    claimed: int
    failed: int


class JobStatsResponse(BaseModel):
    rows: list[JobStatRow]
    total_pending: int
    total_claimed: int
    total_failed: int


@router.get("/stats", response_model=JobStatsResponse)
def get_job_stats(
    session: Annotated[Session, Depends(get_tenant_session)],
) -> JobStatsResponse:
    """Aggregate pending/claimed/failed counts per job type across all libraries."""
    job_repo = WorkerJobRepository(session)
    raw = job_repo.pipeline_status_tenant()
    by_type: dict[str, dict[str, int]] = {}
    for r in raw:
        jt = r["job_type"]
        st = r["status"]
        if st not in ("pending", "claimed", "failed"):
            continue
        if jt not in by_type:
            by_type[jt] = {"pending": 0, "claimed": 0, "failed": 0}
        by_type[jt][st] += r["count"]
    rows = [
        JobStatRow(job_type=jt, pending=c["pending"], claimed=c["claimed"], failed=c["failed"])
        for jt, c in sorted(by_type.items())
    ]
    return JobStatsResponse(
        rows=rows,
        total_pending=sum(r.pending for r in rows),
        total_claimed=sum(r.claimed for r in rows),
        total_failed=sum(r.failed for r in rows),
    )


# ---------------------------------------------------------------------------
# List jobs (for tests / debugging / admin UI)
# ---------------------------------------------------------------------------


class JobListItem(BaseModel):
    job_id: str
    job_type: str
    asset_id: str | None
    status: str
    priority: int
    worker_id: str | None
    fail_count: int
    error_message: str | None
    created_at: str
    claimed_at: str | None
    completed_at: str | None


@router.get("", response_model=list[JobListItem])
def list_jobs(
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[JobListItem]:
    """List jobs, optionally filtered by library_id (via asset), status, with limit.
    By default excludes completed jobs; pass status=completed to see them."""
    from sqlmodel import select
    from sqlalchemy import column
    from sqlalchemy.sql import text as sa_text
    from src.models.tenant import WorkerJob

    active_a = (
        sa_text("SELECT asset_id, library_id FROM active_assets")
        .columns(column("asset_id"), column("library_id"))
        .subquery("active_a")
    )
    if library_id:
        stmt = (
            select(WorkerJob)
            .join(active_a, WorkerJob.asset_id == active_a.c.asset_id)
            .where(active_a.c.library_id == library_id)
        )
    else:
        stmt = select(WorkerJob)
    if status:
        stmt = stmt.where(WorkerJob.status == status)
    else:
        stmt = stmt.where(WorkerJob.status != "completed")
    stmt = stmt.order_by(WorkerJob.created_at.desc()).limit(limit)  # type: ignore[union-attr]
    jobs = list(session.exec(stmt).all())
    return [
        JobListItem(
            job_id=j.job_id,
            job_type=j.job_type,
            asset_id=j.asset_id,
            status=j.status,
            priority=j.priority,
            worker_id=j.worker_id,
            fail_count=j.fail_count,
            error_message=j.error_message,
            created_at=j.created_at.isoformat(),
            claimed_at=j.claimed_at.isoformat() if j.claimed_at else None,
            completed_at=j.completed_at.isoformat() if j.completed_at else None,
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
