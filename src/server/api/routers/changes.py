"""Change reports: what changed on storage, for the brain to scan (ADR-016 phase 2).

The brain mounts the DAS over the network and can't watch it for changes,
so the machine that holds the disks (the Mac Studio) reports the paths it
sees change. Paths are absolute, as that machine sees them, which is how
library roots are stored. The brain's worker reads a library's pending
changes, scans the folders they're in, and acknowledges what it saw.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session

from src.server.api.dependencies import get_tenant_session
from src.server.repository.tenant import LibraryChangeRepository, LibraryRepository

router = APIRouter(tags=["changes"])

MAX_PATHS_PER_REPORT = 10_000
UNMATCHED_SAMPLE = 20


def _clean(path: str) -> str:
    return unicodedata.normalize("NFC", path).rstrip("/") or "/"


class ChangeReport(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=MAX_PATHS_PER_REPORT)

    @field_validator("paths")
    @classmethod
    def _absolute(cls, paths: list[str]) -> list[str]:
        for p in paths:
            if not p.startswith("/"):
                raise ValueError(f"paths must be absolute: {p!r}")
            if ".." in p.split("/"):
                raise ValueError(f"paths may not contain '..': {p!r}")
        return paths


class ChangeReportResponse(BaseModel):
    accepted: int
    libraries: dict[str, int]
    unmatched: int
    unmatched_sample: list[str]


class PendingChange(BaseModel):
    change_id: str
    rel_path: str
    reported_at: datetime
    version: int


class PendingChangesResponse(BaseModel):
    changes: list[PendingChange]
    truncated: bool


class SeenChange(BaseModel):
    change_id: str
    version: int


class AckRequest(BaseModel):
    changes: list[SeenChange] = Field(default_factory=list, max_length=MAX_PATHS_PER_REPORT)


class AckResponse(BaseModel):
    acknowledged: int


class LibraryChangeSummary(BaseModel):
    library_id: str
    pending: int
    oldest_reported_at: datetime


class ChangeSummaryResponse(BaseModel):
    libraries: list[LibraryChangeSummary]


def _match(path: str, roots: list[tuple[str, str]]) -> tuple[str, str] | None:
    """(library_id, rel_path) for the deepest library root holding path."""
    best: tuple[str, str, int] | None = None
    for library_id, root in roots:
        hit = root == "/" or path == root or path.startswith(root + "/")
        if hit and (best is None or len(root) > best[2]):
            rel = "" if path == root else path[len(root):].lstrip("/")
            best = (library_id, rel, len(root))
    return (best[0], best[1]) if best else None


@router.post("/v1/changes", response_model=ChangeReportResponse)
def report_changes(
    body: ChangeReport,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> ChangeReportResponse:
    """Record paths that changed on storage. Paths in no library are counted, not kept."""
    roots = [
        (lib.library_id, _clean(lib.root_path))
        for lib in LibraryRepository(session).list_all()
        if lib.status != "trashed" and lib.root_path
    ]
    by_library: dict[str, list[str]] = {}
    unmatched: list[str] = []
    for raw in body.paths:
        hit = _match(_clean(raw), roots)
        if hit is None:
            unmatched.append(raw)
            continue
        by_library.setdefault(hit[0], []).append(hit[1])

    repo = LibraryChangeRepository(session)
    for library_id, rel_paths in by_library.items():
        repo.record(library_id, rel_paths)
    return ChangeReportResponse(
        accepted=sum(len(set(v)) for v in by_library.values()),
        libraries={k: len(set(v)) for k, v in by_library.items()},
        unmatched=len(unmatched),
        unmatched_sample=unmatched[:UNMATCHED_SAMPLE],
    )


@router.get("/v1/changes", response_model=ChangeSummaryResponse)
def change_summary(
    session: Annotated[Session, Depends(get_tenant_session)],
) -> ChangeSummaryResponse:
    """Pending change counts per library, for a worker deciding what to scan."""
    return ChangeSummaryResponse(
        libraries=[LibraryChangeSummary(**row) for row in LibraryChangeRepository(session).summary()],
    )


def _library_or_404(session: Session, library_id: str) -> None:
    if LibraryRepository(session).get_by_id(library_id) is None:
        raise HTTPException(status_code=404, detail="Library not found")


@router.get("/v1/libraries/{library_id}/changes", response_model=PendingChangesResponse)
def pending_changes(
    library_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    limit: Annotated[int, Query(ge=1, le=MAX_PATHS_PER_REPORT)] = MAX_PATHS_PER_REPORT,
) -> PendingChangesResponse:
    """A library's pending changes, oldest first. truncated means more are waiting."""
    _library_or_404(session, library_id)
    rows, truncated = LibraryChangeRepository(session).pending(library_id, limit)
    return PendingChangesResponse(changes=[PendingChange(**r) for r in rows], truncated=truncated)


@router.post("/v1/libraries/{library_id}/changes/ack", response_model=AckResponse)
def acknowledge_changes(
    library_id: str,
    body: AckRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> AckResponse:
    """Clear changes a scan covered. A change reported again since it was read stays."""
    _library_or_404(session, library_id)
    removed = LibraryChangeRepository(session).acknowledge(
        library_id, [(c.change_id, c.version) for c in body.changes]
    )
    return AckResponse(acknowledged=removed)
