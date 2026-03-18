"""Search sync API. All routes require tenant auth."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.core.config import get_settings
from src.repository.tenant import LibraryRepository, SearchSyncQueueRepository
from src.workers.search_sync import SearchSyncWorker

router = APIRouter(prefix="/v1/search-sync", tags=["search-sync"])


# ---------------------------------------------------------------------------
# Pending count
# ---------------------------------------------------------------------------


class PendingResponse(BaseModel):
    count: int


@router.get("/pending", response_model=PendingResponse)
def get_pending(
    library_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    path_prefix: str | None = None,
) -> PendingResponse:
    """Return count of pending (including expired-processing) search sync rows for a library."""
    settings = get_settings()
    ssq_repo = SearchSyncQueueRepository(session)
    count = ssq_repo.pending_count(
        library_id=library_id,
        path_prefix=path_prefix,
        lease_minutes=settings.search_sync_lease_minutes,
    )
    return PendingResponse(count=count)


# ---------------------------------------------------------------------------
# Process one batch
# ---------------------------------------------------------------------------


class ProcessBatchRequest(BaseModel):
    library_id: str
    path_prefix: str | None = None
    batch_size: int = 100


class ProcessBatchResponse(BaseModel):
    processed: bool
    synced: int
    skipped: int


@router.post("/process-batch", response_model=ProcessBatchResponse)
def process_batch(
    body: ProcessBatchRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> ProcessBatchResponse:
    """
    Claim and process one batch from the search sync queue for a library.

    Returns processed=false when the queue is empty (no work claimed).
    The CLI loop should keep calling this until processed=false.
    """
    library_repo = LibraryRepository(session)
    if library_repo.get_by_id(body.library_id) is None:
        raise HTTPException(status_code=404, detail="Library not found")

    worker = SearchSyncWorker(
        session=session,
        library_id=body.library_id,
        path_prefix=body.path_prefix,
        batch_size=body.batch_size,
    )
    result = worker.process_one_batch()
    return ProcessBatchResponse(
        processed=bool(result["processed"]),
        synced=int(result["synced"]),
        skipped=int(result["skipped"]),
    )


# ---------------------------------------------------------------------------
# Force resync (re-enqueue all)
# ---------------------------------------------------------------------------


class ResyncRequest(BaseModel):
    library_id: str
    path_prefix: str | None = None


class ResyncResponse(BaseModel):
    enqueued: int


@router.post("/resync", response_model=ResyncResponse)
def resync(
    body: ResyncRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> ResyncResponse:
    """
    Re-enqueue all assets for a library into the search sync queue.
    Equivalent to `lumiverb worker search-sync --force-resync`.
    """
    library_repo = LibraryRepository(session)
    if library_repo.get_by_id(body.library_id) is None:
        raise HTTPException(status_code=404, detail="Library not found")

    ssq_repo = SearchSyncQueueRepository(session)
    asset_ids = ssq_repo.enqueue_all_for_library(
        library_id=body.library_id,
        path_prefix=body.path_prefix,
    )
    return ResyncResponse(enqueued=len(asset_ids))
