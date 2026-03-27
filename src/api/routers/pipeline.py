"""Pipeline lock and status API. All routes require tenant auth."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.repository.tenant import (
    AssetRepository,
    LibraryRepository,
    PipelineLockHeldError,
    PipelineLockRepository,
    WorkerJobRepository,
)

router = APIRouter(prefix="/v1/pipeline", tags=["pipeline"])

_STAGE_ORDER = [
    "proxy",
    "exif",
    "ai_vision",
    "embed",
    "video-index",
    "video-vision",
    "video-preview",
]

_JOB_TYPE_LABEL: dict[str, str] = {
    "proxy": "Proxy",
    "exif": "EXIF",
    "ai_vision": "Vision (AI)",
    "embed": "Embeddings",
    "video-index": "Video Index",
    "video-vision": "Video Vision",
    "video-preview": "Video Preview",
}


# ---------------------------------------------------------------------------
# Lock: acquire / heartbeat / release
# ---------------------------------------------------------------------------


class LockAcquireRequest(BaseModel):
    lock_timeout_minutes: int = 5
    force: bool = False


class LockAcquireResponse(BaseModel):
    lock_id: str
    tenant_id: str


@router.post("/lock/acquire", response_model=LockAcquireResponse)
def acquire_lock(
    body: LockAcquireRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> LockAcquireResponse:
    """
    Acquire the pipeline lock for the tenant.

    Returns the lock_id on success. Include lock_id in the release request to
    prevent accidental deletion of a lock reacquired by another process.

    Returns 409 if a fresh lock is held by another process (and force=False).
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Tenant context missing")

    lock_repo = PipelineLockRepository(session)
    try:
        if body.force:
            lock_id = lock_repo.force_acquire(tenant_id)
        else:
            lock_repo.try_acquire(tenant_id, lock_timeout_minutes=body.lock_timeout_minutes)
            lock_id = lock_repo.get_lock_id(tenant_id)
    except PipelineLockHeldError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "lock_held",
                "message": f"Pipeline lock held by {e.hostname} pid={e.pid} since {e.started_at}",
                "details": {
                    "hostname": e.hostname,
                    "pid": e.pid,
                    "started_at": e.started_at.isoformat() if e.started_at else None,
                },
            },
        )
    return LockAcquireResponse(lock_id=lock_id, tenant_id=tenant_id)


@router.post("/lock/heartbeat", status_code=204)
def heartbeat_lock(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> Response:
    """Update the pipeline lock heartbeat for the tenant."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Tenant context missing")
    lock_repo = PipelineLockRepository(session)
    lock_repo.heartbeat(tenant_id)
    return Response(status_code=204)


class LockReleaseRequest(BaseModel):
    lock_id: str | None = None


@router.post("/lock/release", status_code=204)
def release_lock(
    body: LockReleaseRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> Response:
    """
    Release the pipeline lock for the tenant.

    If lock_id is provided, the lock is only deleted if the stored lock_id matches.
    This prevents a crashed-and-restarted supervisor from releasing a lock that was
    reacquired by a new process.
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Tenant context missing")
    lock_repo = PipelineLockRepository(session)
    lock_repo.release(tenant_id, lock_id=body.lock_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Pipeline status
# ---------------------------------------------------------------------------


def _build_pivot(
    job_rows: list[dict],
) -> dict[str, dict[str, int]]:
    """Build stage-name → {done, inflight, pending, failed, blocked} from raw repo rows."""
    pivot: dict[str, dict[str, int]] = {}
    for r in job_rows:
        jt, sv, count = r["job_type"], r["status"], r["count"]
        if jt not in pivot:
            pivot[jt] = {"done": 0, "inflight": 0, "pending": 0, "failed": 0, "blocked": 0}
        if sv == "completed":
            pivot[jt]["done"] += count
        elif sv == "claimed":
            pivot[jt]["inflight"] += count
        elif sv == "pending":
            pivot[jt]["pending"] += count
        elif sv == "failed":
            pivot[jt]["failed"] += count
        elif sv == "blocked":
            pivot[jt]["blocked"] += count
    return pivot


def _pivot_to_stages(pivot: dict[str, dict[str, int]]) -> list[dict]:
    stages = []
    for name in _STAGE_ORDER:
        if name not in pivot:
            continue
        c = pivot[name]
        total = c["done"] + c["inflight"] + c["pending"] + c["failed"] + c.get("blocked", 0)
        if total == 0:
            continue
        stages.append(
            {
                "name": name,
                "label": _JOB_TYPE_LABEL.get(name, name),
                "done": c["done"],
                "inflight": c["inflight"],
                "pending": c["pending"],
                "failed": c["failed"],
                "blocked": c.get("blocked", 0),
            }
        )
    return stages


@router.get("/status")
def get_pipeline_status(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str | None = None,
) -> dict:
    """
    Return pipeline status in the same JSON shape as `lumiverb status --output json`.

    With library_id: single-library payload with stages list.
    Without library_id: tenant-wide payload with a libraries list.
    """
    library_repo = LibraryRepository(session)
    asset_repo = AssetRepository(session)
    job_repo = WorkerJobRepository(session)

    all_libraries = library_repo.list_all()

    if library_id is not None:
        target = next((lib for lib in all_libraries if lib.library_id == library_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="Library not found")

        job_rows = job_repo.pipeline_status(library_id)
        active_workers = job_repo.active_worker_count(library_id=library_id)
        total_assets = asset_repo.count_by_library(library_id)

        pivot = _build_pivot(job_rows)
        return {
            "library": target.name,
            "library_id": library_id,
            "total_assets": total_assets,
            "workers": active_workers,
            "stages": _pivot_to_stages(pivot),
        }

    # Tenant-wide
    active_workers = job_repo.active_worker_count()
    job_rows_all = job_repo.pipeline_status_tenant()

    per_lib_pivot: dict[str, dict[str, dict[str, int]]] = {lib.library_id: {} for lib in all_libraries}

    for r in job_rows_all:
        lid, jt, sv, count = r["library_id"], r["job_type"], r["status"], r["count"]
        if lid not in per_lib_pivot:
            continue
        if jt not in per_lib_pivot[lid]:
            per_lib_pivot[lid][jt] = {"done": 0, "inflight": 0, "pending": 0, "failed": 0, "blocked": 0}
        if sv == "completed":
            per_lib_pivot[lid][jt]["done"] += count
        elif sv == "claimed":
            per_lib_pivot[lid][jt]["inflight"] += count
        elif sv == "pending":
            per_lib_pivot[lid][jt]["pending"] += count
        elif sv == "failed":
            per_lib_pivot[lid][jt]["failed"] += count
        elif sv == "blocked":
            per_lib_pivot[lid][jt]["blocked"] += count

    libraries = []
    for lib in all_libraries:
        pivot = per_lib_pivot.get(lib.library_id, {})
        libraries.append(
            {
                "library": lib.name,
                "library_id": lib.library_id,
                "total_assets": asset_repo.count_by_library(lib.library_id),
                "stages": _pivot_to_stages(pivot),
            }
        )

    return {"workers": active_workers, "libraries": libraries}
