"""Scan management API. All routes require tenant auth (middleware)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.repository.tenant import AssetRepository, LibraryRepository, ScanRepository

router = APIRouter(prefix="/v1/scans", tags=["scans"])


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
    files_discovered: int
    files_added: int
    files_updated: int
    files_skipped: int


class CompleteScanResponse(BaseModel):
    scan_id: str
    files_missing: int


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


@router.post("/{scan_id}/complete", response_model=CompleteScanResponse)
def complete_scan(
    scan_id: str,
    body: CompleteScanRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> CompleteScanResponse:
    """
    Mark scan complete, update library scan_status and last_scan_at.
    Mark assets not seen in this scan as missing; return files_missing count.
    """
    scan_repo = ScanRepository(session)
    lib_repo = LibraryRepository(session)
    asset_repo = AssetRepository(session)
    scan = scan_repo.get_by_id(scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="Scan not found")
    if scan.status != "running":
        raise HTTPException(status_code=409, detail="Scan is not running")
    files_missing = asset_repo.mark_missing_for_scan(scan.library_id, scan_id)
    counts = {
        "files_discovered": body.files_discovered,
        "files_added": body.files_added,
        "files_updated": body.files_updated,
        "files_skipped": body.files_skipped,
        "files_missing": files_missing,
    }
    scan_repo.complete(scan_id, counts)
    lib_repo.update_scan_status(scan.library_id, "complete")
    return CompleteScanResponse(scan_id=scan_id, files_missing=files_missing)


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
