"""Scan management API. All routes require tenant auth (middleware)."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.repository.tenant import AssetRepository, LibraryRepository, ScanRepository

router = APIRouter(prefix="/v1/scans", tags=["scans"])


class BatchItem(BaseModel):
    """One scan action: skip, update, missing, or add."""

    action: str
    asset_id: str | None = None
    rel_path: str | None = None
    file_size: int | None = None
    file_mtime: str | None = None
    media_type: str | None = None


class BatchRequest(BaseModel):
    items: list[BatchItem] = Field(default_factory=list)


class BatchResponse(BaseModel):
    added: int
    updated: int
    skipped: int
    missing: int


class CreateScanRequest(BaseModel):
    library_id: str
    status: str  # running | aborted | error
    root_path_override: str | None = None
    worker_id: str | None = None
    error_message: str | None = None


class CreateScanResponse(BaseModel):
    scan_id: str


class RunningScanItem(BaseModel):
    scan_id: str
    library_id: str
    started_at: str
    worker_id: str | None


class CompleteScanRequest(BaseModel):
    """Optional for backward compat; counts now accumulated server-side via batch endpoint."""

    files_discovered: int | None = None
    files_added: int | None = None
    files_updated: int | None = None
    files_skipped: int | None = None


class CompleteScanResponse(BaseModel):
    scan_id: str
    files_discovered: int
    files_added: int
    files_updated: int
    files_skipped: int
    files_missing: int
    status: str


class AbortScanRequest(BaseModel):
    error_message: str | None = None


class AbortScanResponse(BaseModel):
    scan_id: str
    status: str


@router.post("", response_model=CreateScanResponse)
def create_scan(
    body: CreateScanRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> CreateScanResponse:
    """
    Create a scan record. If status is running, set library scan_status to scanning.
    If status is aborted or error, update library scan_status and last_scan_error.
    """
    scan_repo = ScanRepository(session)
    lib_repo = LibraryRepository(session)
    library = lib_repo.get_by_id(body.library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    scan = scan_repo.create(
        library_id=body.library_id,
        root_path_override=body.root_path_override,
        worker_id=body.worker_id,
        status=body.status,
        error_message=body.error_message,
    )
    if body.status == "running":
        lib_repo.update_scan_status(body.library_id, "scanning")
    elif body.status in ("aborted", "error"):
        lib_repo.update_scan_status(body.library_id, body.status, error=body.error_message)
    return CreateScanResponse(scan_id=scan.scan_id)


@router.get("/running", response_model=list[RunningScanItem])
def get_running_scans(
    library_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> list[RunningScanItem]:
    """Return list of running scans for the given library_id."""
    scan_repo = ScanRepository(session)
    scans = scan_repo.get_running_scans(library_id)
    return [
        RunningScanItem(
            scan_id=s.scan_id,
            library_id=s.library_id,
            started_at=s.started_at.isoformat(),
            worker_id=s.worker_id,
        )
        for s in scans
    ]


@router.post("/{scan_id}/batch", response_model=BatchResponse)
def batch_scan(
    scan_id: str,
    body: BatchRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> BatchResponse:
    """
    Process a batch of scan actions: skip, update, missing, add.
    All in one transaction. Accumulates counts on scan record.
    """
    scan_repo = ScanRepository(session)
    asset_repo = AssetRepository(session)
    scan = scan_repo.get_by_id(scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="Scan not found")
    if scan.status != "running":
        raise HTTPException(status_code=409, detail="Scan is not running")

    added = updated = skipped = missing = 0
    skip_ids: list[str] = []
    update_items: list[dict] = []
    missing_ids: list[str] = []
    add_items: list[dict] = []

    for item in body.items:
        a = item.action
        if a == "skip" and item.asset_id:
            skip_ids.append(item.asset_id)
        elif a == "update" and item.asset_id and item.file_size is not None and item.file_mtime:
            update_items.append({
                "asset_id": item.asset_id,
                "file_size": item.file_size,
                "file_mtime": item.file_mtime,
                "media_type": item.media_type,
            })
        elif a == "missing" and item.asset_id:
            missing_ids.append(item.asset_id)
        elif a == "add" and item.rel_path and item.file_size is not None and item.file_mtime and item.media_type:
            add_items.append({
                "rel_path": item.rel_path,
                "file_size": item.file_size,
                "file_mtime": item.file_mtime,
                "media_type": item.media_type,
            })

    skipped = asset_repo.touch_for_scan_bulk(skip_ids, scan_id) if skip_ids else 0
    if update_items:
        parsed: list[dict] = []
        for it in update_items:
            file_mtime_dt: datetime | None = None
            if it.get("file_mtime"):
                try:
                    file_mtime_dt = datetime.fromisoformat(it["file_mtime"].replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass
            parsed.append({
                "asset_id": it["asset_id"],
                "file_size": it["file_size"],
                "file_mtime": file_mtime_dt,
                "media_type": it.get("media_type"),
            })
        updated = asset_repo.update_for_scan_bulk(parsed, scan_id)
    missing = asset_repo.set_missing_bulk(missing_ids, scan_id) if missing_ids else 0
    added = asset_repo.create_or_update_for_scan_bulk(
        library_id=scan.library_id,
        scan_id=scan_id,
        items=add_items,
    ) if add_items else 0

    scan_repo.record_batch_counts(scan_id, added=added, updated=updated, skipped=skipped, missing=missing)
    return BatchResponse(added=added, updated=updated, skipped=skipped, missing=missing)


@router.post("/{scan_id}/complete", response_model=CompleteScanResponse)
def complete_scan(
    scan_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    body: CompleteScanRequest | None = Body(default=None),
) -> CompleteScanResponse:
    """
    Mark scan complete, update library scan_status and last_scan_at.
    Mark assets not seen in this scan as missing. Counts are accumulated
    server-side via batch endpoint; body fields ignored if sent (backward compat).
    """
    scan_repo = ScanRepository(session)
    lib_repo = LibraryRepository(session)
    asset_repo = AssetRepository(session)
    scan = scan_repo.get_by_id(scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="Scan not found")
    if scan.status != "running":
        raise HTTPException(status_code=409, detail="Scan is not running")
    batch_missing = scan.files_missing or 0
    complete_time_missing = asset_repo.mark_missing_for_scan(scan.library_id, scan_id)
    total_missing = batch_missing + complete_time_missing
    counts = {
        "files_discovered": scan.files_discovered or 0,
        "files_added": scan.files_added or 0,
        "files_updated": scan.files_updated or 0,
        "files_skipped": scan.files_skipped or 0,
        "files_missing": total_missing,
    }
    scan_repo.complete(scan_id, counts)
    lib_repo.update_scan_status(scan.library_id, "complete")
    scan = scan_repo.get_by_id(scan_id)
    assert scan is not None
    return CompleteScanResponse(
        scan_id=scan_id,
        files_discovered=scan.files_discovered or 0,
        files_added=scan.files_added or 0,
        files_updated=scan.files_updated or 0,
        files_skipped=scan.files_skipped or 0,
        files_missing=total_missing,
        status="complete",
    )


@router.post("/{scan_id}/abort", response_model=AbortScanResponse)
def abort_scan(
    scan_id: str,
    body: AbortScanRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> AbortScanResponse:
    """Abort a running scan; update library scan_status to error or aborted."""
    scan_repo = ScanRepository(session)
    lib_repo = LibraryRepository(session)
    scan = scan_repo.get_by_id(scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="Scan not found")
    scan = scan_repo.abort(scan_id, error_message=body.error_message)
    status = "error" if body.error_message else "aborted"
    lib_repo.update_scan_status(scan.library_id, status, error=body.error_message)
    return AbortScanResponse(scan_id=scan_id, status=scan.status)
